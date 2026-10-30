"""The Claude Code wake bridge over a real Windows named pipe.

This process plays the Claude session: it registers itself in a session registry the way Claude
Code does on Windows (procStart as a FILETIME) and serves the inbox pipe. The fake-pipe tests in
test_claude_code_bridge.py cover the same rules on every platform.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from uuid import uuid4

import pytest

from local_ai_connector import os_adapter

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes")

BRIDGE_PATH = Path(__file__).resolve().parents[1] / "integrations/claude_code/bridge.py"
spec = importlib.util.spec_from_file_location("claude_code_bridge_windows", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
TOKEN = "test-token"


@pytest.fixture
def session(tmp_path):
    """This process as a registered Claude session whose inbox is a fresh named pipe."""
    session_id, pipe = str(uuid4()), rf"\\.\pipe\lac-claude-test-{uuid4().hex}"
    sessions = tmp_path / "sessions"
    os_adapter.ensure_private_dir(sessions)
    started = os_adapter.process_start_time(os.getpid())
    os_adapter.create_private_file(sessions / f"{os.getpid()}.json", json.dumps({
        "pid": os.getpid(), "sessionId": session_id, "messagingSocketPath": pipe, "status": "idle",
        "procStart": str(int(started * 10_000_000) + 116444736000000000)}).encode())
    return {"id": session_id, "pipe": pipe, "sessions": sessions}


def request(session, op="send", credential=None):
    dispatch = str(uuid4())
    fields = {"dispatch_id": dispatch, "text": "wake " + dispatch} if op == "send" else {}
    if credential is not None:
        fields["credential"] = credential
    return {"op": op, "target": {"session": "claude:" + session["id"]}, **fields}


def test_status_reads_the_registry_without_a_token_or_a_connection(session, monkeypatch):
    monkeypatch.setattr(os_adapter, "connect_local_ipc", lambda *_: pytest.fail("must not connect"))
    assert bridge.handle(request(session, "status"), sessions_dir=session["sessions"]) == {"ok": True, "state": "idle"}
    reply = bridge.handle(request(session), sessions_dir=session["sessions"])
    assert reply["error"] == "unavailable" and "token" in reply["detail"]


def test_send_authenticates_with_the_chats_token_then_writes_one_frame(session):
    import win32file
    import win32pipe

    server = win32pipe.CreateNamedPipe(
        session["pipe"], win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT, 1, 65536, 65536, 0, None)
    received = []

    def serve():
        win32pipe.ConnectNamedPipe(server, None)
        data = b""
        while data.count(b"\n") < 2:
            data += win32file.ReadFile(server, 65536)[1]
        received.extend(json.loads(line) for line in data.splitlines())
        server.Close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    wake = request(session, credential={"token": TOKEN, "inbox": session["pipe"]})
    reply = bridge.handle(wake, sessions_dir=session["sessions"])
    thread.join(5)
    assert not thread.is_alive()
    assert received[0] == {"type": "auth", "token": TOKEN}
    assert received[1]["msg_id"] == wake["dispatch_id"] and received[1]["session_id"] == session["id"]
    assert reply["error"] == "ambiguous" and TOKEN not in json.dumps(reply)
