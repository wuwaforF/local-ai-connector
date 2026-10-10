"""Installer: isolation, idempotent setup, collision detection and owned-only uninstall."""
from dataclasses import replace
import json
from pathlib import Path
import socket
import threading

import pytest
import tomlkit

from hostfakes import context
from local_ai_connector import install as installer, os_adapter, service
from local_ai_connector.hosts import HOSTS, HostConfigError
from local_ai_connector.install import InstallError, doctor, profile_dir, read_manifest, setup, uninstall
from local_ai_connector.wakeup import load_config

CODEX_CONFIG = """# my Codex settings
model = "gpt-6"

[mcp_servers.other_tool]  # keep me
command = "other"
args = ["--flag"]
"""


def codex_file(ctx):
    return HOSTS["codex"].config_path(ctx.host_env)


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_setup_creates_a_private_installation_and_preserves_unrelated_configuration(tmp_path):
    ctx = context(tmp_path)
    write(codex_file(ctx), CODEX_CONFIG)
    report = setup("codex", ctx=ctx, start_service=False)
    data = profile_dir("default", ctx)
    assert report["changed"] and [a["action"] for a in report["actions"]] == ["create_installation", "write_host_entry"]
    text = codex_file(ctx).read_text()
    assert text.startswith(CODEX_CONFIG.rstrip("\n"))  # comments and other servers untouched
    entry = tomlkit.parse(text)["mcp_servers"]["local_ai_connector"]
    assert entry["args"][-3:] == ["--config", str(data / "codex.json"), "--start-service"]
    assert "tools" not in entry and "approval_mode" not in text  # no tool permission is granted
    for name in ("server.json", "codex.json", "claude.json", "antigravity.json", "install.json"):
        assert os_adapter.is_private(data / name), name
    assert os_adapter.is_private(data)
    endpoints = {h: json.loads((data / f"{h}.json").read_text()) for h in HOSTS}
    assert endpoints["codex"]["chat_identity"] == {"source": "codex_meta"}
    assert endpoints["claude"]["approval_transport"] == "host_tool_permission"
    assert endpoints["antigravity"]["chat_identity"] == {"source": "meta", "path": ["antigravity.google/conversation_id"],
                                                         "namespace": "antigravity"}
    assert data.parent.parent == os_adapter.app_data_dir(environ=ctx.environ, home=ctx.home)


def test_repeated_setup_changes_nothing(tmp_path):
    ctx = context(tmp_path)
    write(codex_file(ctx), CODEX_CONFIG)
    first = setup("codex", ctx=ctx, start_service=False)
    before = codex_file(ctx).read_bytes()
    manifest = read_manifest(profile_dir("default", ctx))
    second = setup("codex", ctx=ctx, start_service=False)
    assert second["changed"] is False and second["actions"] == []
    assert codex_file(ctx).read_bytes() == before
    assert read_manifest(profile_dir("default", ctx)) == manifest
    assert second["installation_id"] == first["installation_id"]


def test_dry_run_writes_nothing(tmp_path):
    ctx = context(tmp_path)
    write(codex_file(ctx), CODEX_CONFIG)
    report = setup("codex", ctx=ctx, dry_run=True)
    assert [a["action"] for a in report["actions"]] == ["create_installation", "write_host_entry"]
    assert codex_file(ctx).read_text() == CODEX_CONFIG
    assert not profile_dir("default", ctx).exists()


def test_foreign_entries_and_data_are_detected_and_profiles_install_side_by_side(tmp_path):
    ctx = context(tmp_path)
    live = CODEX_CONFIG + '\n[mcp_servers.local_ai_connector]\ncommand = "/live/python"\nargs = ["-m", "x"]\n'
    write(codex_file(ctx), live)
    with pytest.raises(InstallError) as error:
        setup("codex", ctx=ctx, start_service=False)
    assert error.value.code == "entry_collision"
    assert codex_file(ctx).read_text() == live and not profile_dir("default", ctx).exists()
    side = setup("codex", profile="next", ctx=ctx, start_service=False)
    assert side["entry"] == "local_ai_connector_next"
    servers = tomlkit.parse(codex_file(ctx).read_text())["mcp_servers"]
    assert servers["local_ai_connector"]["command"] == "/live/python" and "local_ai_connector_next" in servers
    # A data directory this installer did not create is never adopted.
    foreign = profile_dir("other", ctx)
    foreign.mkdir(parents=True)
    (foreign / "server.json").write_text("{}")
    with pytest.raises(InstallError) as error:
        setup("antigravity", profile="other", ctx=ctx, start_service=False)
    assert error.value.code == "data_dir_collision"


def test_user_edits_are_kept_and_never_overwritten_or_removed(tmp_path):
    ctx = context(tmp_path)
    setup("codex", ctx=ctx, start_service=False)
    # The user allows a tool in Codex: their sub-table stays and the entry is still ours.
    document = tomlkit.parse(codex_file(ctx).read_text())
    tools = tomlkit.table()
    tools["connector_receive"] = {"approval_mode": "approve"}
    document["mcp_servers"]["local_ai_connector"]["tools"] = tools
    codex_file(ctx).write_text(tomlkit.dumps(document))
    assert setup("codex", ctx=ctx, start_service=False)["changed"] is False
    assert "approval_mode" in codex_file(ctx).read_text()
    # A changed launch command is the user's: setup refuses and uninstall leaves it.
    document["mcp_servers"]["local_ai_connector"]["command"] = "/my/python"
    codex_file(ctx).write_text(tomlkit.dumps(document))
    with pytest.raises(InstallError) as error:
        setup("codex", ctx=ctx, start_service=False)
    assert error.value.code == "entry_modified"
    report = uninstall(ctx=ctx)
    assert report["host_entries"][0]["state"] == "modified_left_in_place"
    assert "local_ai_connector" in tomlkit.parse(codex_file(ctx).read_text())["mcp_servers"]
    with pytest.raises(InstallError) as error:
        uninstall(ctx=ctx, purge=True)
    assert error.value.code == "entries_remaining" and profile_dir("default", ctx).exists()


def test_an_unmodified_entry_follows_a_new_runtime(tmp_path):
    ctx = context(tmp_path)
    setup("antigravity", ctx=ctx, start_service=False)
    moved = context(tmp_path, runtime=str(tmp_path / "new-runtime" / "python"))
    report = setup("antigravity", ctx=moved, start_service=False)
    assert report["actions"] == [{"action": "write_host_entry", "host": "antigravity", "entry": "local_ai_connector",
                                  "file": str(HOSTS["antigravity"].config_path(ctx.host_env))}]
    entry = json.loads(HOSTS["antigravity"].config_path(ctx.host_env).read_text())["mcpServers"]["local_ai_connector"]
    assert entry["command"] == moved.runtime


def test_uninstall_removes_only_owned_entries_then_purges_owned_data(tmp_path):
    ctx = context(tmp_path)
    antigravity = HOSTS["antigravity"].config_path(ctx.host_env)
    write(antigravity, json.dumps({"mcpServers": {"keep": {"command": "x"}}, "other": True}))
    write(codex_file(ctx), CODEX_CONFIG)
    setup("antigravity", ctx=ctx, start_service=False)
    setup("codex", ctx=ctx, start_service=False)
    setup("claude", ctx=ctx, start_service=False)
    assert json.loads((ctx.home / ".claude.json").read_text())["mcpServers"]["local_ai_connector"]["type"] == "stdio"
    report = uninstall(ctx=ctx, purge=True)
    assert {r["host"]: r["state"] for r in report["host_entries"]} == {
        "antigravity": "removed", "codex": "removed", "claude": "removed"}
    assert json.loads(antigravity.read_text()) == {"mcpServers": {"keep": {"command": "x"}}, "other": True}
    assert tomlkit.parse(codex_file(ctx).read_text())["mcp_servers"].unwrap() == {
        "other_tool": {"command": "other", "args": ["--flag"]}}
    assert json.loads((ctx.home / ".claude.json").read_text())["mcpServers"] == {}
    assert not profile_dir("default", ctx).exists() and report["service"] == "not_running"


def test_claude_uses_its_cli_and_refuses_shadowing_or_missing_cli(tmp_path):
    ctx = context(tmp_path)
    write(ctx.home / ".claude.json", json.dumps({"projects": {"/work/a": {"mcpServers": {"local_ai_connector": {}}}}}))
    with pytest.raises(InstallError) as error:
        setup("claude", ctx=ctx, start_service=False)
    assert error.value.code == "entry_collision" and "/work/a" in str(error.value)
    assert not profile_dir("default", ctx).exists()
    ctx2 = context(tmp_path / "second")
    del ctx2.environ["LOCAL_AI_CONNECTOR_CLAUDE_CLI"]
    ctx2.environ["PATH"] = str(tmp_path / "empty")
    with pytest.raises(HostConfigError) as error:
        setup("claude", ctx=ctx2, start_service=False)
    assert error.value.code == "host_cli_missing"
    calls = [json.loads(line) for line in (ctx.home / "claude-cli.log").read_text().splitlines()] \
        if (ctx.home / "claude-cli.log").exists() else []
    assert calls == []


def test_port_owned_by_another_program_is_reported_not_taken_over(tmp_path):
    ctx = context(tmp_path)
    setup("codex", ctx=ctx, start_service=False)
    data = profile_dir("default", ctx)
    server = json.loads((data / "server.json").read_text())
    endpoint = json.loads((data / "codex.json").read_text())
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", server["port"]))
    squatter.listen()

    def answer():
        while True:
            try:
                connection, _ = squatter.accept()
            except OSError:
                return
            connection.recv(4096)
            connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            connection.close()
    threading.Thread(target=answer, daemon=True).start()
    try:
        assert service.probe(server["url"], endpoint["token"]) == "foreign"
        with pytest.raises(service.ServiceError) as error:
            service.ensure(data, server["url"], endpoint["token"])
        assert error.value.code == "port_collision"
        report = doctor(ctx=ctx)
        assert report["ready"] is False
        assert {c["check"]: c["ok"] for c in report["checks"]}["service"] is False
    finally:
        squatter.close()


def test_doctor_reports_each_check_and_what_it_could_not_verify(tmp_path):
    ctx = context(tmp_path)
    assert doctor(ctx=ctx)["ready"] is False
    setup("codex", ctx=ctx, start_service=False)
    setup("antigravity", ctx=ctx, start_service=False)
    report = doctor(ctx=ctx)
    checks = {c["check"]: c for c in report["checks"]}
    assert report["ready"] is True
    assert checks["codex.entry"]["ok"] is True and checks["private_files"]["ok"] is True
    assert checks["codex.host_loaded"]["evidence"] == "not checked"
    assert checks["service"]["ok"] is None  # starts on demand with the first MCP connection


# --- automatic wake-up (opt-in, Codex on macOS) ---------------------------------------------
def mac(tmp_path):
    return replace(context(tmp_path), platform="darwin")


def wake_config(ctx):
    return json.loads((profile_dir("default", ctx) / "wakeup.json").read_text())


def rewrite_wake(ctx, config):
    os_adapter.replace_private_file(profile_dir("default", ctx) / "wakeup.json", json.dumps(config).encode())


def test_wake_is_opt_in_and_writes_a_pinned_binding_the_service_loads(tmp_path):
    ctx = mac(tmp_path)
    assert setup("codex", ctx=ctx, start_service=False)["wake"] == "off"
    data = profile_dir("default", ctx)
    assert not (data / "wakeup.json").exists()

    report = setup("codex", ctx=ctx, start_service=False, wake=True)
    assert [a["action"] for a in report["actions"]] == ["write_wake_config"] and report["wake"] == "enabled"
    assert os_adapter.is_private(data / "wakeup.json")
    bridge = installer.SOURCE_ROOT / "integrations" / "codex_desktop" / "bridge.py"
    assert wake_config(ctx) == {"enabled": True, "bindings": {"codex": {
        "adapter": "command", "target": "pinned", "timeout": 30, "restore": False,
        "command": [ctx.runtime, str(bridge), "--state-dir", str(data / "codex-desktop-dispatches")]}}}
    bindings, options = load_config(data, set(HOSTS))
    assert bindings["codex"].pinned and options == {}

    assert setup("codex", ctx=ctx, start_service=False)["changed"] is False  # a plain re-run keeps it
    assert setup("codex", ctx=ctx, start_service=False, wake=True)["changed"] is False
    moved = replace(ctx, runtime=str(tmp_path / "new-python"))
    assert "write_wake_config" in [a["action"] for a in setup("codex", ctx=moved, start_service=False)["actions"]]
    assert wake_config(ctx)["bindings"]["codex"]["command"][0] == moved.runtime


def test_wake_refuses_other_hosts_platforms_and_bindings_it_does_not_own(tmp_path):
    ctx = mac(tmp_path)
    for host, platform, code in (("antigravity", "darwin", "wake_unsupported"),
                                 ("codex", "linux", "wake_unsupported_platform")):
        with pytest.raises(InstallError) as error:
            setup(host, ctx=replace(ctx, platform=platform), start_service=False, wake=True)
        assert error.value.code == code
        assert not profile_dir("default", replace(ctx, platform=platform)).exists()  # checked before any write

    setup("codex", ctx=ctx, start_service=False)
    foreign = {"enabled": True, "bindings": {"codex": {"adapter": "command", "target": "pinned", "command": ["mine"]}}}
    os_adapter.create_private_file(profile_dir("default", ctx) / "wakeup.json", json.dumps(foreign).encode())
    with pytest.raises(InstallError) as error:
        setup("codex", ctx=ctx, start_service=False, wake=True)
    assert error.value.code == "wake_collision" and wake_config(ctx) == foreign

    (profile_dir("default", ctx) / "wakeup.json").unlink()
    setup("codex", ctx=ctx, start_service=False, wake=True)
    edited = wake_config(ctx)
    edited["bindings"]["codex"]["restore"] = True
    rewrite_wake(ctx, edited)
    with pytest.raises(InstallError) as error:
        setup("codex", ctx=ctx, start_service=False, wake=True)
    assert error.value.code == "wake_modified"
    assert setup("codex", ctx=ctx, start_service=False)["changed"] is False and wake_config(ctx) == edited


def test_no_wake_and_uninstall_remove_only_the_owned_binding(tmp_path):
    ctx = mac(tmp_path)
    setup("codex", ctx=ctx, start_service=False, wake=True)
    report = setup("codex", ctx=ctx, start_service=False, wake=False)
    assert [a["action"] for a in report["actions"]] == ["remove_wake_config"] and report["wake"] == "off"
    assert not (profile_dir("default", ctx) / "wakeup.json").exists()  # no bindings left

    setup("codex", ctx=ctx, start_service=False, wake=True)
    theirs = {"adapter": "command", "target": {"session_id": "x"}, "command": ["mine"]}
    config = wake_config(ctx)
    config["bindings"]["claude"] = theirs
    rewrite_wake(ctx, config)
    result = uninstall(ctx=ctx)
    assert result["host_entries"][0]["wake"] == "removed"
    assert wake_config(ctx)["bindings"] == {"claude": theirs}
    assert read_manifest(profile_dir("default", ctx))["wake"] == {}


def test_doctor_reports_wake_up(tmp_path):
    ctx = mac(tmp_path)
    setup("codex", ctx=ctx, start_service=False)
    wake = {c["check"]: c for c in doctor(ctx=ctx)["checks"]}["codex.wake"]
    assert wake["ok"] is None and "setup codex --wake" in wake["detail"]
    setup("codex", ctx=ctx, start_service=False, wake=True)
    assert {c["check"]: c for c in doctor(ctx=ctx)["checks"]}["codex.wake"]["ok"] is True
    config = wake_config(ctx)
    config["enabled"] = False
    rewrite_wake(ctx, config)
    wake = {c["check"]: c for c in doctor(ctx=ctx)["checks"]}["codex.wake"]
    assert wake["ok"] is False and "turned off" in wake["detail"]
    assert setup("codex", ctx=ctx, start_service=False)["changed"] is False  # a plain re-run respects that
    assert [a["action"] for a in setup("codex", ctx=ctx, start_service=False, wake=True)["actions"]] == ["write_wake_config"]
    assert wake_config(ctx)["enabled"] is True


def test_changing_wake_up_restarts_a_running_service(tmp_path, monkeypatch):
    ctx = mac(tmp_path)
    setup("codex", ctx=ctx, start_service=False)
    calls = []
    monkeypatch.setattr(service, "probe", lambda url, token: "ours")
    monkeypatch.setattr(service, "stop", lambda data, **kw: calls.append("stop") or "stopped")
    monkeypatch.setattr(service, "ensure", lambda *a, **kw: calls.append("ensure") or "started")
    report = setup("codex", ctx=ctx, start_service=False, wake=True)
    assert calls == ["stop", "ensure"] and report["service_restarted"]
    calls.clear()
    setup("codex", ctx=ctx, start_service=False, wake=True)
    assert calls == []
