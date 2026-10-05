"""Exercise the owner-run script with fake service management and HTTP, never launchd."""
import json
import asyncio
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest
import httpx

from local_ai_connector.cli import main
from local_ai_connector.core import Broker

SCRIPT = Path(__file__).parents[1] / "deployment/enable-peer-chat-approval.command"


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root = tmp_path / "package"
    root.mkdir()
    home = tmp_path / "home"
    data = home / ".local/share/local-ai-connector"
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(data), "init", "--peers", "claude_code", "gemini", "gpt"])
    main()
    (data / "wakeup.json").write_text('{"bindings":{}}')
    Broker(data / "state.sqlite3").close()
    expected = [str(root / ".venv/bin/python"), "-m", "local_ai_connector.cli", "--data", str(data), "serve"]
    plist_path = home / "Library/LaunchAgents/dev.local-ai-connector.service.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(plistlib.dumps({"Label": "dev.local-ai-connector.service", "ProgramArguments": expected,
                                         "EnvironmentVariables": {"PYTHONPATH": str(root / "src")}, "KeepAlive": True, "RunAtLoad": True}))
    state = {"loaded": True, "foreign": False, "commands": [], "fail_bootout": False, "fail_bootstrap": False,
             "http_connect_failures": 0}
    def output(argv, **kwargs):
        if argv[:2] == ["/bin/launchctl", "list"]:
            return "123\t0\tdev.local-ai-connector.service\n" if state["loaded"] else ""
        if argv[0] == "/bin/ps":
            return str(os.getuid()) if argv[-1] == "uid=" else " ".join(expected)
        raise AssertionError(argv)
    def run(argv, **kwargs):
        if argv[:2] == ["/bin/launchctl", "print"]:
            return SimpleNamespace(returncode=0 if state["loaded"] else 1)
        if argv[0] == "/usr/sbin/lsof":
            return SimpleNamespace(returncode=0 if state["loaded"] else 1,
                                   stdout="999\n" if state["foreign"] else ("123\n" if state["loaded"] else ""), stderr="")
        state["commands"].append(argv)
        if argv[:2] == ["/bin/launchctl", "bootout"]:
            if state["fail_bootout"]:
                raise subprocess.CalledProcessError(1, argv)
            state["loaded"] = False
        elif argv[:2] == ["/bin/launchctl", "bootstrap"]:
            if state["fail_bootstrap"]:
                raise subprocess.CalledProcessError(1, argv)
            state["loaded"] = True
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0)
    class Http:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, path, *, headers, json):
            if state["http_connect_failures"]:
                state["http_connect_failures"] -= 1
                raise httpx.ConnectError("synthetic startup race")
            assert path == "/call" and json == {"action": "status"}
            server = __import__("json").loads((data / "server.json").read_text())
            peer = next(p for p, token in server["peers"].items() if headers["Authorization"] == "Bearer " + token)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"self": peer})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", output)
    monkeypatch.setattr("httpx.Client", Http)
    monkeypatch.setattr("time.sleep", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["-", str(root)])
    source = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    return data, state, lambda: exec(compile(source, str(SCRIPT), "exec"), {})


def test_script_stops_upgrades_recovers_and_repeats_idempotently(deployment):
    data, state, execute = deployment
    before = {name: (data / name).read_bytes() for name in ("gpt.json", "wakeup.json")}
    execute()
    first = {path.name: path.read_bytes() for path in data.glob("*.json")}
    assert state["loaded"]
    assert [cmd[1] for cmd in state["commands"]] == ["bootout", "bootstrap"]
    for peer in ("claude_code", "gemini"):
        assert json.loads(first[f"{peer}.json"])["tool_profile"] == "participant"
    assert all((data / name).read_bytes() == content for name, content in before.items())
    execute()
    assert {path.name: path.read_bytes() for path in data.glob("*.json")} == first


def test_script_refuses_unrelated_listener_without_mutation(deployment):
    data, state, execute = deployment
    before = (data / "server.json").read_bytes()
    state["foreign"] = True
    with pytest.raises(RuntimeError, match="listener"):
        execute()
    assert not state["commands"] and (data / "server.json").read_bytes() == before


def test_failed_stop_does_not_upgrade_or_bootstrap_over_live_job(deployment):
    data, state, execute = deployment
    before = (data / "server.json").read_bytes()
    state["fail_bootout"] = True
    with pytest.raises(subprocess.CalledProcessError):
        execute()
    assert state["loaded"] and (data / "server.json").read_bytes() == before
    assert [cmd[1] for cmd in state["commands"]] == ["bootout"]


def test_failed_migration_recovers_original_service(deployment, monkeypatch):
    data, state, execute = deployment
    before = (data / "server.json").read_bytes()
    def fail(*args, **kwargs): raise ValueError("synthetic migration failure")
    monkeypatch.setattr("local_ai_connector.registry.enable_chat_approval", fail)
    with pytest.raises(ValueError, match="synthetic"):
        execute()
    assert state["loaded"] and (data / "server.json").read_bytes() == before
    assert [cmd[1] for cmd in state["commands"]] == ["bootout", "bootstrap"]


def test_failed_restart_reports_recovery_and_retains_completed_migration(deployment, capsys):
    data, state, execute = deployment
    state["fail_bootstrap"] = True
    with pytest.raises(SystemExit) as error:
        execute()
    assert error.value.code == 1
    assert "Recovery:" in capsys.readouterr().err
    assert json.loads((data / "claude_code.json").read_text())["tool_profile"] == "participant"
    assert not state["loaded"]


def test_pending_task_refuses_before_stopping_service(deployment):
    data, state, execute = deployment
    config = json.loads((data / "server.json").read_text())
    broker = Broker(data / "state.sqlite3")
    try:
        for peer, token in config["peers"].items():
            broker.register(peer, token)
        asyncio.run(broker.open("claude_code", "gemini", "Pending task", "pending-test"))
    finally:
        broker.close()
    before = (data / "server.json").read_bytes()
    with pytest.raises(RuntimeError, match="pending or active"):
        execute()
    assert not state["commands"] and (data / "server.json").read_bytes() == before


def test_connection_refused_during_startup_is_retried(deployment):
    data, state, execute = deployment
    state["http_connect_failures"] = 1
    execute()
    assert state["loaded"] and state["http_connect_failures"] == 0

