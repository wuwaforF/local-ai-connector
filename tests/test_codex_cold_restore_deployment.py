import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import types

import pytest

pytestmark = pytest.mark.macos  # macOS deployment scripts, launchd or Desktop paths


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deployment/enable-codex-desktop-cold-restore.command"
SOURCE = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
PEER = "codex_desktop"
THREAD = "0190f2c1-7a3b-7c4d-8e5f-6a7b8c9d0e1f"
WORKSPACE = "/workspaces/dedicated-codex-worker"


class _CommandAdapter:
    pass


class _Binding:
    def __init__(self, target, restore):
        self.target = target
        self.restore_enabled = restore
        self.adapter = _CommandAdapter()


@pytest.mark.parametrize("fail_enable", [False, True], ids=["success", "rollback"])
def test_cold_restore_owner_script_lifecycle(tmp_path, monkeypatch, fail_enable):
    data = tmp_path / ".local/share/local-ai-connector"
    data.mkdir(parents=True)
    python = ROOT / ".venv/bin/python"
    bridge = ROOT / "integrations/codex_desktop/bridge.py"
    target = {"thread_id": THREAD, "workspace": WORKSPACE}
    wake = {
        "enabled": True,
        "restore_timeout_seconds": 60,
        "another_option": {"preserved": True},
        "bindings": {
            PEER: {"adapter": "command", "target": target,
                   "command": [str(python), str(bridge)], "timeout": 30},
            "gpt": {"adapter": "other", "target": {"preserved": True}},
        },
    }
    server = {"port": 19171, "url": "http://127.0.0.1:19171", "peers": {PEER: {}}}
    (data / "server.json").write_text(json.dumps(server))
    original_wakeup = (json.dumps(wake) + "\n").encode()
    (data / "wakeup.json").write_bytes(original_wakeup)
    (data / f"{PEER}.json").write_text(json.dumps({"bound_session": f"codex:{THREAD}"}))
    with sqlite3.connect(data / "state.sqlite3") as db:
        db.executescript(
            "CREATE TABLE channels(id TEXT, status TEXT);"
            "CREATE TABLE messages(channel TEXT, kind TEXT, resolved INTEGER);"
        )

    plist_path = tmp_path / "Library/LaunchAgents/dev.local-ai-connector.service.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(plistlib.dumps({
        "Label": "dev.local-ai-connector.service",
        "ProgramArguments": [str(python), "-m", "local_ai_connector.cli", "--data", str(data), "serve"],
        "EnvironmentVariables": {"PYTHONPATH": str(ROOT / "src")},
        "KeepAlive": True,
        "RunAtLoad": True,
    }))

    state = {"loaded": True, "pid": 4101, "failed_enable_once": False}
    def run(args, **kwargs):
        args = list(map(str, args))
        if args[0] == str(python):
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True, "state": "idle"}), stderr="")
        if args[:3] == ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/dev.local-ai-connector.service"]:
            state["loaded"] = False
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}"]:
            state["loaded"] = True
            state["pid"] += 1
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["/bin/launchctl", "kickstart", "-k"]:
            state["pid"] += 1
            return subprocess.CompletedProcess(args, 0)
        if args[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(
                args, 0 if state["loaded"] else 1,
                stdout=f"{state['pid']}\n" if state["loaded"] else "", stderr="",
            )
        raise AssertionError(f"Unexpected subprocess.run: {args}")

    def check_output(args, **kwargs):
        args = list(map(str, args))
        if args == ["/bin/launchctl", "list"]:
            return f"{state['pid']}\t0\tdev.local-ai-connector.service\n" if state["loaded"] else ""
        if args[:2] == ["/bin/ps", "-p"]:
            return f"{os.getuid()}\n"
        if args[:2] == ["/bin/ps", "-ww"]:
            return " ".join([str(python), "-m", "local_ai_connector.cli", "--data", str(data), "serve"]) + "\n"
        raise AssertionError(f"Unexpected subprocess.check_output: {args}")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(ROOT), "--thread-id", THREAD,
                                      "--workspace", WORKSPACE, "--apply"])

    wakeup_module = types.ModuleType("local_ai_connector.wakeup")
    wakeup_module.CommandAdapter = _CommandAdapter

    def load_config(_data, _peers):
        restore = json.loads((data / "wakeup.json").read_text())["bindings"][PEER].get("restore", False)
        if restore and fail_enable and not state["failed_enable_once"]:
            state["failed_enable_once"] = True
            raise RuntimeError("injected post-write validation failure")
        return {PEER: _Binding(target, restore)}, object()

    wakeup_module.load_config = load_config
    monkeypatch.setitem(sys.modules, "local_ai_connector.wakeup", wakeup_module)

    if fail_enable:
        with pytest.raises(RuntimeError, match="injected post-write validation failure"):
            exec(compile(SOURCE, str(SCRIPT), "exec"), {"__name__": "__main__"})
        expected_restore = False
    else:
        exec(compile(SOURCE, str(SCRIPT), "exec"), {"__name__": "__main__"})
        expected_restore = True

    result = json.loads((data / "wakeup.json").read_text())
    assert result["bindings"][PEER].get("restore", False) is expected_restore
    assert {k: v for k, v in result.items() if k != "bindings"} == {
        k: v for k, v in wake.items() if k != "bindings"
    }
    assert {k: v for k, v in result["bindings"].items() if k != PEER} == {
        k: v for k, v in wake["bindings"].items() if k != PEER
    }
    backups = list(data.glob("wakeup.json.backup-codex-cold-restore-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_wakeup
    assert state["loaded"] is True
    assert state["pid"] != 4101
