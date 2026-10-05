import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import sqlite3
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("conversation_deployment", ROOT / "integrations/codex_desktop/conversation_deployment.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


@pytest.fixture
def installation(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    project = tmp_path / "worker"
    (project / ".codex").mkdir(parents=True)
    thread = "11111111-1111-4111-8111-111111111111"
    server = {"port": 19001, "peers": {"codex_desktop": "private-test-token", "other": "preserve"},
              "approval_tokens": {"other": "preserve-approval"}}
    (data / "server.json").write_text(json.dumps(server))
    (data / "codex_desktop.json").write_text(json.dumps({"peer": "codex_desktop", "token": "private-test-token",
                                                       "bound_session": "codex:" + thread}))
    (data / "wakeup.json").write_text(json.dumps({"bindings": {"codex_desktop": {
        "command": [str(ROOT / ".venv/bin/python"), str(ROOT / "integrations/codex_desktop/bridge.py")],
        "target": {"thread_id": thread, "workspace": str(project)}}}}))
    command = shlex.join([str(ROOT / ".venv/bin/python"), str(ROOT / "integrations/codex_desktop/creation_approval.py"),
                         "--store", str(data / "codex-creation-probe-g/grants.sqlite3"), "--grant-id", "codex-create-20260929-g"])
    (project / ".codex/hooks.json").write_text(json.dumps({"hooks": {"PermissionRequest": [{
        "matcher": "^mcp__codex_app__create_thread$", "hooks": [{"type": "command", "command": command, "timeout": 10}]}]}}))
    (project / ".codex/config.toml").write_text(
        '[mcp_servers.local_ai_connector_codex_desktop_worker]\n'
        f'command = {json.dumps(str(ROOT / ".venv/bin/python"))}\n'
        f'args = {json.dumps(["-m", "local_ai_connector.cli", "mcp", "--config", str(data / "codex_desktop.json")])}\n')
    with sqlite3.connect(data / "state.sqlite3") as db:
        db.executescript("CREATE TABLE channels(id TEXT, status TEXT); CREATE TABLE messages(channel TEXT,kind TEXT,resolved INTEGER);")
    plist = tmp_path / "service.plist"
    plist.write_bytes(plistlib.dumps({"Label": "dev.local-ai-connector.service",
        "ProgramArguments": [str(ROOT / ".venv/bin/python"), "-m", "local_ai_connector.cli", "--data", str(data), "serve"],
        "EnvironmentVariables": {"PYTHONPATH": str(ROOT / "src")}}))
    return data, project, plist


def test_prepare_is_narrow_idempotent_and_refuses_unknown_hook(installation):
    data, project, _ = installation
    original, updated = deployment.prepare(ROOT, data, "codex_desktop")
    server = json.loads(updated[data / "server.json"])
    assert server.pop("conversations")["codex_desktop"]["provider"] == "codex_ingress"
    assert server == json.loads(original[data / "server.json"])
    assert b'approval_mode = "prompt"' in updated[project / ".codex/config.toml"]
    for path, value in updated.items():
        path.write_bytes(value)
    again, same = deployment.prepare(ROOT, data, "codex_desktop")
    assert again == same
    (project / ".codex/hooks.json").write_text('{"hooks": {}}')
    with pytest.raises(ValueError, match="Hooks differ"):
        deployment.prepare(ROOT, data, "codex_desktop")


@pytest.mark.parametrize("failure", [None, "write", "bootstrap"])
def test_lifecycle_releases_lock_and_recovers_failed_install(installation, monkeypatch, failure):
    data, _, plist = installation
    originals, replacements = deployment.prepare(ROOT, data, "codex_desktop")
    held_lock = (data / "server.lock").open("a")
    fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = {"pid": "101", "loaded": True, "failed": False}

    def run(args, **kwargs):
        if args[1] == "bootout":
            state["loaded"] = False
            fcntl.flock(held_lock, fcntl.LOCK_UN)
        elif args[1] == "bootstrap":
            if failure == "bootstrap" and not state["failed"]:
                state["failed"] = True
                raise subprocess.CalledProcessError(1, args)
            state["loaded"] = True
            state["pid"] = "102"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0)

    original_replace = deployment.replace
    def replace(path, content):
        if failure == "write" and path.name == "hooks.json" and not state["failed"]:
            state["failed"] = True
            raise OSError("injected write failure")
        original_replace(path, content)

    monkeypatch.setattr(deployment, "replace", replace)
    monkeypatch.setattr(deployment, "listener", lambda *_: state["pid"] if state["loaded"] else None)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: "101\t0\tdev.local-ai-connector.service\n")
    try:
        if failure:
            with pytest.raises((OSError, subprocess.CalledProcessError)):
                deployment.apply(ROOT, data, originals, replacements, plist)
            assert all(path.read_bytes() == content for path, content in originals.items())
        else:
            deployment.apply(ROOT, data, originals, replacements, plist)
            assert all(path.read_bytes() == content for path, content in replacements.items())
        assert state["loaded"] and state["pid"] == "102"
        backup = next(data.glob("native-conversations-backup-*"))
        assert json.loads((backup / "manifest.json").read_text()) == [str(p) for p in originals]
        for i, (path, content) in enumerate(originals.items()):
            saved = backup / f"{i}-{path.name}"
            assert saved.read_bytes() == content and saved.stat().st_mode & 0o777 == 0o600
    finally:
        held_lock.close()


def test_pending_work_prevents_service_changes(installation):
    data, _, plist = installation
    originals, replacements = deployment.prepare(ROOT, data, "codex_desktop")
    with sqlite3.connect(data / "state.sqlite3") as db:
        db.execute("INSERT INTO channels VALUES ('task','pending')")
    with pytest.raises(ValueError, match="pending approvals"):
        deployment.apply(ROOT, data, originals, replacements, plist)
    assert all(path.read_bytes() == value for path, value in originals.items())
