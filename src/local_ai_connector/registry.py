"""Owner-managed endpoint registration; credentials remain separate from public profiles."""
import json
import os
from pathlib import Path
import secrets
import tempfile
from contextlib import ExitStack
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from . import os_adapter


MCP_LOCALES = ("zh-CN", "en-US")


def _stage_private(path: Path, content: bytes):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="approval-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        os.unlink(temporary)
        raise
    return Path(temporary)


def enable_chat_approval(data: Path, peers: list[str], *, mcp_locale: str = "en-US"):
    """Give selected worker endpoints their own scoped, host-confirmed requester path."""
    from .cli import peer_name
    from .identity import LEGACY_IDENTITIES, validate_identity
    if not peers or len(set(peers)) != len(peers):
        raise ValueError("Select one or more distinct registered endpoints")
    for peer in peers:
        peer_name(peer)
    if mcp_locale not in MCP_LOCALES:
        raise ValueError("mcp_locale must be zh-CN or en-US")
    with ExitStack() as locks:
        try:
            locks.enter_context(os_adapter.exclusive_lock(data / "server.lock"))
        except os_adapter.LockBusy:
            raise ValueError("Stop the connector service before enabling chat approval")
        # A surviving stdio client can still pin its identity while the broker is stopped.
        for peer in sorted(peers):
            locks.enter_context(os_adapter.exclusive_lock(data / f"{peer}.lock", blocking=True))
        paths = [data / "server.json", *(data / f"{peer}.json" for peer in peers)]
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ValueError("Configuration must exist as regular files, not symbolic links")
        original = {path: path.read_bytes() for path in paths}
        config, *endpoints = [json.loads(original[path]) for path in paths]
        if not isinstance(config, dict):
            raise ValueError("Server configuration must be an object")
        registered = config.get("peers")
        if (not isinstance(registered, dict) or not isinstance(config.get("admin_token"), str)
                or not config["admin_token"] or not isinstance(config.get("url"), str)
                or any(not isinstance(value, str) or not value for value in registered.values())):
            raise ValueError("Invalid server credentials or address")
        approvals = config.get("approval_tokens", {})
        if (not isinstance(approvals, dict)
                or any(peer not in registered or not isinstance(token, str) or not token
                       for peer, token in approvals.items())
                or len(set(approvals.values())) != len(approvals)
                or any(token == config["admin_token"] or token in registered.values()
                       for token in approvals.values())):
            raise ValueError("Invalid or overlapping scoped approval credentials")
        used = {config["admin_token"], *registered.values(), *approvals.values()}
        for peer, endpoint in zip(peers, endpoints):
            if (not isinstance(endpoint, dict) or peer not in registered
                    or endpoint.get("peer") != peer or endpoint.get("token") != registered[peer]
                    or endpoint.get("url") != config["url"]):
                raise ValueError(f"Endpoint identity or credentials do not match the server: {peer}")
            client = endpoint.get("client", "generic")
            if client == "metadata":
                validate_identity(endpoint.get("identity"))
            elif client not in ("generic", *LEGACY_IDENTITIES):
                raise ValueError(f"Unsupported endpoint client: {peer}")
            profile = endpoint.get("tool_profile", "peer")
            if profile == "participant":
                if not approvals.get(peer) or endpoint.get("approval_token") != approvals[peer]:
                    raise ValueError(f"Participant approval credentials do not match: {peer}")
            elif profile == "peer":
                if "approval_token" in endpoint or peer in approvals:
                    raise ValueError(f"Peer already has inconsistent approval configuration: {peer}")
                token = secrets.token_urlsafe(32)
                while token in used:
                    token = secrets.token_urlsafe(32)
                used.add(token)
                approvals[peer] = endpoint["approval_token"] = token
            else:
                raise ValueError(f"Only peer or participant profiles can be upgraded: {peer}")
            endpoint.update(tool_profile="participant", mcp_locale=mcp_locale)
        config["approval_tokens"] = approvals
        updated = dict(zip(paths, [config, *endpoints]))
        staged, backups, committed, rollback_errors = {}, {}, [], []
        try:
            for path, value in updated.items():
                content = json.dumps(value, ensure_ascii=False, indent=2).encode()
                if json.loads(original[path]) == value and os_adapter.is_private(path):
                    continue
                staged[path] = _stage_private(path, content)
                backups[path] = _stage_private(path, original[path])
            for path, temporary in staged.items():
                os.replace(temporary, path)
                committed.append(path)
        except BaseException as failure:
            for path in reversed(committed):
                try:
                    os.replace(backups[path], path)
                except OSError as exc:
                    rollback_errors.append(RuntimeError(f"Restore {path} from {backups[path]}: {exc}"))
            if rollback_errors:
                raise BaseExceptionGroup("Approval migration failed; recovery copies were retained",
                                         [failure, *rollback_errors])
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
            # A failed restoration must retain the original bytes for owner recovery.
            if not rollback_errors:
                for temporary in backups.values():
                    temporary.unlink(missing_ok=True)
        return paths[1:]


class PeerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=1000)
    capabilities: list[Annotated[str, Field(strict=True, min_length=1, max_length=100)]] = Field(default_factory=list, max_length=30)


def add_peer(data: Path, peer: str, profile: dict, identity: dict | None = None, *, mcp_locale: str = "zh-CN"):
    from .cli import peer_name, save_private
    from .identity import validate_identity
    peer_name(peer)
    if mcp_locale not in MCP_LOCALES:
        raise ValueError("mcp_locale must be zh-CN or en-US")
    profile = PeerProfile.model_validate(profile).model_dump()
    if identity is not None:
        validate_identity(identity)
    # Registration changes multiple startup files. Serialize with the service owner so
    # no running server can observe half a registration or keep stale authentication.
    with ExitStack() as held:
        try:
            held.enter_context(os_adapter.exclusive_lock(data / "server.lock"))
        except os_adapter.LockBusy:
            raise ValueError("Stop the connector service before registering an endpoint")
        config_path = data / "server.json"
        config = json.loads(config_path.read_text())
        peer_path = data / f"{peer}.json"
        if peer in config["peers"] or peer_path.exists():
            raise ValueError("Endpoint already exists; existing credentials were preserved")
        token = secrets.token_urlsafe(32)
        endpoint = {"url": config["url"], "token": token, "peer": peer,
                    "client": "metadata" if identity else "generic", "mcp_locale": mcp_locale}
        if identity:
            endpoint["identity"] = identity
        config["peers"][peer] = token
        config.setdefault("peer_profiles", {})[peer] = profile
        fd, temporary = tempfile.mkstemp(dir=data, prefix="registry-", suffix=".tmp")
        created_peer = False
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(config, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            save_private(peer_path, endpoint)
            created_peer = True
            os.replace(temporary, config_path)
        except BaseException:
            if created_peer:
                peer_path.unlink()
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return peer_path
