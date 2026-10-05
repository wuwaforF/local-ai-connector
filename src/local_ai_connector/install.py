"""Installation profiles: isolated, idempotent setup; uninstall removes only owned resources.

An installation is a profile directory under the platform's per-user application directory,
holding an ownership manifest (install.json). Setup records every host entry it writes, with
a fingerprint of the launch keys it owns. Later runs and uninstall act on an entry only while
it still matches that record, so foreign or user-edited configuration is never overwritten.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import time
from uuid import uuid4

import httpx

from . import os_adapter, service
from .hosts import HOSTS, HostEnv, fingerprint

SCHEMA = 1
MANIFEST = "install.json"
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ENDPOINTS = tuple(HOSTS)  # one endpoint per host, all created with the installation


class InstallError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Context:
    home: Path
    environ: dict
    runtime: str = sys.executable
    platform: str = sys.platform

    @classmethod
    def current(cls):
        return cls(Path.home(), dict(os.environ))

    @property
    def host_env(self) -> HostEnv:
        return HostEnv(self.home, self.environ)


def profile_dir(profile: str, ctx: Context) -> Path:
    if not _PROFILE.fullmatch(profile):
        raise InstallError("invalid_profile", "A profile name is 1 to 32 lowercase letters, digits or hyphens.")
    return os_adapter.app_data_dir(platform=ctx.platform, environ=ctx.environ, home=ctx.home) / "profiles" / profile


def entry_name(profile: str) -> str:
    return "local_ai_connector" if profile == "default" else "local_ai_connector_" + profile.replace("-", "_")


def mcp_args(data: Path, host: str) -> list[str]:
    return ["-m", "local_ai_connector.cli", "mcp", "--config", str(data / f"{host}.json"), "--start-service"]


def _dumps(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode()


def read_manifest(data: Path) -> dict | None:
    """The installation at data, None if there is none, or an error if data is not ours."""
    if not data.exists():
        return None
    path = data / MANIFEST
    if not path.is_file():
        if any(data.iterdir()):
            raise InstallError("data_dir_collision",
                               f"{data} exists and was not created by this installer; nothing was changed.")
        return None
    try:
        os_adapter.check_private(data)
        os_adapter.check_private(path)
    except (os_adapter.NotPrivate, OSError) as exc:
        raise InstallError("data_dir_not_private", f"{data} is not private to this user: {exc}") from None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
            or not isinstance(manifest.get("installation_id"), str) or manifest.get("data_dir") != str(data)):
        raise InstallError("data_dir_collision", f"The manifest in {data} belongs to another installation.")
    return manifest


def _write_manifest(data: Path, manifest: dict):
    os_adapter.replace_private_file(data / MANIFEST, _dumps(manifest))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _create(data: Path, ctx: Context, profile: str) -> dict:
    """Build the installation in a staging directory and move it into place in one rename."""
    data.parent.mkdir(parents=True, exist_ok=True)
    staging = data.parent / f".{profile}.{secrets.token_hex(4)}.creating"
    os_adapter.ensure_private_dir(staging)
    try:
        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        tokens = {host: secrets.token_urlsafe(32) for host in ENDPOINTS}
        approvals = {host: secrets.token_urlsafe(32) for host in ENDPOINTS}
        server = {"url": url, "port": port, "admin_token": secrets.token_urlsafe(32), "peers": tokens,
                  "approval_tokens": approvals,
                  "peer_profiles": {host: {"name": HOSTS[host].DISPLAY_NAME, "description": "", "capabilities": []}
                                    for host in ENDPOINTS}}
        os_adapter.create_private_file(staging / "server.json", _dumps(server))
        for host in ENDPOINTS:
            spec = HOSTS[host].ENDPOINT
            endpoint = {"url": url, "token": tokens[host], "peer": host, "client": "generic",
                        "tool_profile": "participant", "approval_token": approvals[host], "mcp_locale": "en-US",
                        "approval_transport": spec["approval_transport"]}
            if spec["chat_identity"] is not None:
                endpoint["chat_identity"] = spec["chat_identity"]
            os_adapter.create_private_file(staging / f"{host}.json", _dumps(endpoint))
        manifest = {"schema": SCHEMA, "installation_id": str(uuid4()), "profile": profile, "created": time.time(),
                    "data_dir": str(data), "runtime": ctx.runtime, "port": port, "endpoints": list(ENDPOINTS),
                    "host_entries": {}}
        os_adapter.create_private_file(staging / MANIFEST, _dumps(manifest))
        os.rename(staging, data)  # a partial installation never appears at the profile path
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _entry_state(adapter, name: str, desired: dict, record: dict | None, env: HostEnv) -> str:
    current = adapter.read_entry(name, env)
    if current is None:
        return "absent"
    owned = fingerprint(adapter.owned_view(current))
    if owned == fingerprint(adapter.owned_view(desired)):
        return "current"
    if record is not None:
        return "outdated" if owned == record["fingerprint"] else "modified"
    return "foreign"


def _backup(data: Path, path: Path, host: str):
    if not path.exists() or not getattr(HOSTS[host], "EDITS_CONFIG_DIRECTLY", True):
        return
    backups = data / "backups"
    os_adapter.ensure_private_dir(backups)
    os_adapter.create_private_file(backups / f"{host}-{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}-{path.name}",
                                   path.read_bytes())


def _remove_tree(path: Path, wait: float = 10.0):
    """Delete an owned directory; Windows can keep a just-exited service's files open briefly."""
    deadline = time.monotonic() + wait
    while True:
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def setup(host: str, *, profile: str = "default", ctx: Context | None = None, dry_run: bool = False,
          start_service: bool = True) -> dict:
    ctx = ctx or Context.current()
    adapter = HOSTS.get(host)
    if adapter is None:
        raise InstallError("unknown_host", f"Unknown host '{host}'. Choose one of: {', '.join(HOSTS)}.")
    env, name, data = ctx.host_env, entry_name(profile), profile_dir(profile, ctx)
    manifest = read_manifest(data)
    # Every collision check runs before anything is written.
    record = manifest["host_entries"].get(host) if manifest else None
    desired = adapter.launch_entry(ctx.runtime, mcp_args(data, host))
    state = _entry_state(adapter, name, desired, record, env)
    config_file = adapter.config_path(env)
    if state == "foreign":
        raise InstallError("entry_collision",
                           f"{adapter.DISPLAY_NAME} already has an MCP server named '{name}' that this installation did "
                           f"not create ({config_file}). Nothing was changed. Use --profile to install side by side.")
    shadowed = getattr(adapter, "collisions", lambda *_: [])(name, env)
    if shadowed:
        raise InstallError("entry_collision",
                           f"{adapter.DISPLAY_NAME} already has a server named '{name}' for: {', '.join(shadowed)}. "
                           "Nothing was changed. Use --profile to install side by side.")
    if state == "modified":
        raise InstallError("entry_modified",
                           f"The '{name}' entry in {config_file} changed since setup wrote it. Nothing was overwritten; "
                           "restore or remove it, then run setup again.")
    actions = []
    if manifest is None:
        actions.append({"action": "create_installation", "data_dir": str(data)})
        if not dry_run:
            manifest = _create(data, ctx, profile)
    if state in ("absent", "outdated"):
        actions.append({"action": "write_host_entry", "host": host, "entry": name, "file": str(config_file)})
    if not dry_run:
        if state in ("absent", "outdated"):
            _backup(data, config_file, host)
            adapter.write_entry(name, desired, env)
        recorded = {"entry": name, "file": str(config_file), "fingerprint": fingerprint(adapter.owned_view(desired))}
        if manifest["host_entries"].get(host) != recorded or manifest["runtime"] != ctx.runtime:
            manifest["host_entries"][host] = recorded
            manifest["runtime"] = ctx.runtime
            _write_manifest(data, manifest)
    report = {"host": host, "profile": profile, "entry": name, "config_file": str(config_file),
              "data_dir": str(data), "dry_run": dry_run, "changed": bool(actions), "actions": actions,
              "permissions": list(adapter.PERMISSIONS), "limitations": list(adapter.LIMITATIONS),
              "next_step": ("In the chat that should receive tasks, ask: \"Use this chat for connector tasks.\""
                            if adapter.ENDPOINT["chat_identity"] else
                            "This host can start tasks; exact-chat binding is not supported for it yet.")}
    if manifest:
        report["installation_id"] = manifest["installation_id"]
    if start_service and not dry_run:
        endpoint = json.loads((data / f"{host}.json").read_text())
        report["service"] = service.ensure(data, endpoint["url"], endpoint["token"], runtime=ctx.runtime)
    return report


def uninstall(*, profile: str = "default", ctx: Context | None = None, hosts: list[str] | None = None,
              purge: bool = False, dry_run: bool = False) -> dict:
    ctx = ctx or Context.current()
    env, data = ctx.host_env, profile_dir(profile, ctx)
    manifest = read_manifest(data)
    if manifest is None:
        raise InstallError("not_installed", f"No installation for profile '{profile}'.")
    results = []
    for host in hosts or list(manifest["host_entries"]):
        record = manifest["host_entries"].get(host)
        if record is None:
            results.append({"host": host, "state": "not_owned"})
            continue
        adapter = HOSTS[host]
        current = adapter.read_entry(record["entry"], env)
        if current is None:
            state = "already_absent"
        elif fingerprint(adapter.owned_view(current)) == record["fingerprint"]:
            state = "removed"
            if not dry_run:
                _backup(data, Path(record["file"]), host)
                adapter.remove_entry(record["entry"], env)
        else:
            state = "modified_left_in_place"
        if state != "modified_left_in_place" and not dry_run:
            del manifest["host_entries"][host]
        results.append({"host": host, "entry": record["entry"], "file": record["file"], "state": state})
    remaining = sorted(manifest["host_entries"])
    if purge and remaining:
        raise InstallError("entries_remaining", "Host entries remain (" + ", ".join(remaining) + "); the data "
                           "directory was kept because they still point to it.")
    report = {"profile": profile, "installation_id": manifest["installation_id"], "dry_run": dry_run,
              "host_entries": results, "remaining_entries": remaining}
    if not dry_run:
        if purge or not remaining:
            report["service"] = service.stop(data)
        if purge:
            _remove_tree(data)
            report["data_dir"] = "removed"
        else:
            _write_manifest(data, manifest)
    return report


def doctor(*, profile: str = "default", ctx: Context | None = None, host: str | None = None) -> dict:
    """Readiness report. Each check says whether it was verified here or could not be checked."""
    ctx = ctx or Context.current()
    env, data = ctx.host_env, profile_dir(profile, ctx)
    checks = []

    def check(name, ok, detail, evidence="verified"):
        checks.append({"check": name, "ok": ok, "detail": detail, "evidence": evidence})

    try:
        manifest = read_manifest(data)
    except InstallError as exc:
        check("installation", False, f"{exc.code}: {exc}")
        return {"profile": profile, "ready": False, "checks": checks}
    if manifest is None:
        check("installation", False, f"No installation at {data}. Run: local-ai-connector setup <host>")
        return {"profile": profile, "ready": False, "checks": checks}
    check("installation", True, f"{data} (installation {manifest['installation_id']})")
    private = [p.name for p in [data, *(data / f"{e}.json" for e in manifest["endpoints"]), data / "server.json"]
               if not os_adapter.is_private(p)]
    check("private_files", not private, "credentials are private to this user" if not private else
          "not private: " + ", ".join(private))
    check("runtime", Path(manifest["runtime"]).exists(), manifest["runtime"])
    server = json.loads((data / "server.json").read_text())
    selected = [host] if host else list(manifest["host_entries"]) or []
    state = None
    for name in selected:
        adapter, record = HOSTS[name], manifest["host_entries"].get(name)
        if record is None:
            check(f"{name}.entry", False, f"not set up; run: local-ai-connector setup {name}")
            continue
        desired = adapter.launch_entry(manifest["runtime"], mcp_args(data, name))
        current = adapter.read_entry(record["entry"], env)
        check(f"{name}.entry", current is not None and
              fingerprint(adapter.owned_view(current)) == fingerprint(adapter.owned_view(desired)),
              f"'{record['entry']}' in {record['file']}")
        check(f"{name}.host_loaded", None, f"restart or reconnect MCP servers in {adapter.DISPLAY_NAME} after setup",
              "not checked")
        token = json.loads((data / f"{name}.json").read_text())["token"]
        state = service.probe(server["url"], token)
        if state != "ours":
            continue
        with httpx.Client(base_url=server["url"], trust_env=False, timeout=5) as http:
            status = http.post("/call", json={"action": "status"}, headers={"Authorization": f"Bearer {token}"}).json()
        worker = next((w for w in status["workers"] if w["id"] == name), {})
        if adapter.ENDPOINT["chat_identity"] is None:
            check(f"{name}.binding", None, "exact-chat binding is not supported for this host", "not supported")
        else:
            binding = worker.get("binding") or {}
            check(f"{name}.binding", True if binding.get("bound") else None,
                  f"bound to '{binding['label']}' ({binding['chat']}, revision {binding['revision']})"
                  if binding.get("bound") else "no chat bound yet; ask in the worker chat: \"Use this chat for connector tasks.\"")
        check(f"{name}.wake", None, "attended mode: the bound chat collects tasks when asked; automatic wake-up "
              "is not available in this version", "not supported")
    if state is None and selected:
        state = service.probe(server["url"], json.loads((data / f"{selected[0]}.json").read_text())["token"])
    if state is not None:
        # Not running is fine: the first MCP connection starts it. A foreign listener is a collision.
        check("service", {"ours": True, "absent": None, "foreign": False}[state],
              {"ours": f"running at {server['url']}", "absent": "not running; it starts with the first MCP connection",
               "foreign": f"another program is using {server['url']}"}[state])
    return {"profile": profile, "ready": all(c["ok"] is not False for c in checks), "checks": checks}
