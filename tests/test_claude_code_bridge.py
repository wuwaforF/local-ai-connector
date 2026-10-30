import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace

import pytest

from local_ai_connector.wakeup import AdapterError, CommandAdapter


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "integrations/claude_code/bridge.py"
spec = importlib.util.spec_from_file_location("claude_code_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
SESSION = "11111111-1111-4111-8111-111111111111"
DISPATCH = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def inbox():
    # Keep fake Unix socket paths below macOS's path-length limit.
    with tempfile.TemporaryDirectory(prefix="cc-bridge-", dir="/tmp") as directory:
        directory = Path(directory)
        path = directory / "inbox.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen(4)
            listener.settimeout(2)
            info = path.stat()
            target = {"session_id": SESSION, "workspace": str(directory)}
            record = {"session_id": SESSION, "workspace": str(directory), "socket": str(path),
                      "socket_dev": info.st_dev, "socket_ino": info.st_ino}
            record_path = directory / f"{SESSION}.json"
            record_path.write_text(json.dumps(record))
            record_path.chmod(0o600)
            yield SimpleNamespace(path=path, listener=listener, target=target,
                                  record=record, record_path=record_path, directory=directory)


def request(inbox, op="send", **fields):
    return {"op": op, "target": inbox.target, "dispatch_id": DISPATCH,
            "text": f"[local-ai-connector wake {DISPATCH}] Retrieve the approved message.", **fields}


def read_frame(inbox):
    with inbox.listener.accept()[0] as connection:
        connection.settimeout(2)
        data = b""
        while chunk := connection.recv(4096):
            data += chunk
    assert data.count(b"\n") == 1 and data.endswith(b"\n")
    return json.loads(data)


def test_read_only_operations_verify_identity_without_connecting(inbox):
    assert bridge.handle(request(inbox, "confirm"), inbox.record_path) == {"ok": True, "target": inbox.target}
    assert bridge.handle(request(inbox, "status"), inbox.record_path) == {"ok": True, "state": "unknown"}
    assert bridge.handle(request(inbox, "reconcile"), inbox.record_path) == {"ok": True, "result": "unknown"}
    inbox.listener.setblocking(False)
    with pytest.raises(BlockingIOError):
        inbox.listener.accept()


def test_one_frame_uses_explicit_target_and_reports_unconfirmed_delivery(inbox):
    sent = request(inbox)
    result = bridge.handle(sent, inbox.record_path)
    assert result["ok"] is False and result["error"] == "ambiguous"
    assert "accepted" not in result and "retryable" not in result
    assert read_frame(inbox) == {
        "msgV": 1, "msg_id": DISPATCH, "type": "user", "priority": "next", "session_id": SESSION,
        "message": {"role": "user", "content": sent["text"]},
    }
    inbox.listener.setblocking(False)
    with pytest.raises(BlockingIOError):
        inbox.listener.accept()


async def test_command_adapter_preserves_ambiguous_result(inbox):
    adapter = CommandAdapter([sys.executable, str(BRIDGE_PATH), "--session-record", str(inbox.record_path)])
    assert await adapter.confirm(inbox.target) == inbox.target
    assert await adapter.status(inbox.target) == "unknown"
    assert await adapter.reconcile(inbox.target, DISPATCH) == "unknown"
    with pytest.raises(AdapterError) as error:
        await adapter.send(inbox.target, DISPATCH, request(inbox)["text"])
    assert error.value.kind == "ambiguous" and error.value.retryable is False
    assert read_frame(inbox)["msg_id"] == DISPATCH


@pytest.mark.parametrize("field,value", [
    ("session_id", "33333333-3333-4333-8333-333333333333"),
    ("workspace", "/another-workspace"), ("session_id", "invalid"), ("session_id", ""),
    ("workspace", "relative-workspace"), ("workspace", 123), ("unexpected", "value"),
])
def test_target_must_match_every_captured_identity_field(inbox, field, value):
    target = {**inbox.target, field: value}
    assert bridge.handle(request(inbox, target=target), inbox.record_path)["error"] == "stale_target"


def test_missing_target_field_is_rejected(inbox):
    del inbox.target["workspace"]
    assert bridge.handle(request(inbox), inbox.record_path)["error"] == "stale_target"


def test_transport_fields_are_not_accepted_as_target_identity(inbox):
    target = {**inbox.target, "socket_path": str(inbox.path),
              "socket_dev": str(inbox.record["socket_dev"]), "socket_ino": str(inbox.record["socket_ino"])}
    assert bridge.handle(request(inbox, target=target), inbox.record_path)["error"] == "stale_target"


@pytest.mark.parametrize("field,value", [
    ("session_id", "33333333-3333-4333-8333-333333333333"), ("workspace", "/another-workspace"),
    ("socket", "/tmp/another-socket"), ("socket", "relative.sock"), ("socket", None),
    ("socket_dev", "1"), ("socket_dev", -1), ("socket_ino", True), ("socket_ino", 0),
])
def test_record_must_confirm_the_target(inbox, field, value):
    inbox.record[field] = value
    inbox.record_path.write_text(json.dumps(inbox.record))
    assert bridge.handle(request(inbox), inbox.record_path)["error"] == "stale_target"


def test_session_record_path_must_explicitly_name_the_session(inbox):
    other_path = inbox.record_path.with_name("another-session.json")
    inbox.record_path.rename(other_path)
    assert bridge.handle(request(inbox), other_path)["error"] == "stale_target"
    assert bridge.handle(request(inbox), Path(inbox.record_path.name))["error"] == "stale_target"


async def test_same_session_resume_uses_new_inbox_without_reconfiguring_adapter(inbox):
    adapter = CommandAdapter([sys.executable, str(BRIDGE_PATH), "--session-record", str(inbox.record_path)])
    target_before = dict(inbox.target)
    with pytest.raises(AdapterError, match="ambiguous"):
        await adapter.send(inbox.target, DISPATCH, request(inbox)["text"])
    assert read_frame(inbox)["session_id"] == SESSION
    resumed_path = inbox.directory / "resumed.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as resumed:
        resumed.bind(str(resumed_path))
        resumed_path.chmod(0o600)
        resumed.listen(4)
        resumed.settimeout(2)
        info = resumed_path.stat()
        inbox.record.update(socket=str(resumed_path), socket_dev=info.st_dev, socket_ino=info.st_ino)
        inbox.record_path.write_text(json.dumps(inbox.record))
        assert await adapter.confirm(inbox.target) == target_before
        resumed_dispatch = "44444444-4444-4444-8444-444444444444"
        with pytest.raises(AdapterError, match="ambiguous"):
            await adapter.send(inbox.target, resumed_dispatch, request(inbox)["text"].replace(DISPATCH, resumed_dispatch))
        frame = read_frame(SimpleNamespace(listener=resumed))
        assert frame["session_id"] == SESSION and frame["msg_id"] == resumed_dispatch
    assert inbox.target == target_before
    inbox.listener.setblocking(False)
    with pytest.raises(BlockingIOError):
        inbox.listener.accept()


@pytest.mark.parametrize("field,value", [
    ("session_id", "33333333-3333-4333-8333-333333333333"), ("workspace", "/another-workspace"),
])
def test_resume_record_cannot_redirect_to_a_different_session_or_workspace(inbox, field, value):
    assert bridge.handle(request(inbox, "confirm"), inbox.record_path)["ok"] is True
    inbox.record[field] = value
    inbox.record_path.write_text(json.dumps(inbox.record))
    assert bridge.handle(request(inbox), inbox.record_path)["error"] == "stale_target"
    inbox.listener.setblocking(False)
    with pytest.raises(BlockingIOError):
        inbox.listener.accept()


@pytest.mark.parametrize("mutation", ["record_mode", "record_symlink", "record_json", "socket_mode", "socket_symlink", "socket_file", "socket_replaced"])
def test_untrusted_or_reused_paths_are_rejected(inbox, mutation):
    if mutation == "record_mode":
        inbox.record_path.chmod(0o644)
    elif mutation == "record_symlink":
        original = inbox.record_path.with_suffix(".original")
        inbox.record_path.rename(original)
        inbox.record_path.symlink_to(original)
    elif mutation == "record_json":
        inbox.record_path.write_text("not JSON")
    elif mutation == "socket_mode":
        inbox.path.chmod(0o660)
    elif mutation == "socket_symlink":
        original = inbox.path.with_suffix(".original")
        inbox.path.rename(original)
        inbox.path.symlink_to(original)
    elif mutation == "socket_file":
        inbox.path.unlink()
        inbox.path.write_text("")
        inbox.path.chmod(0o600)
    else:
        # Create the replacement before unlinking the old inode so its identity must differ.
        replacement = inbox.path.with_suffix(".replacement")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
            other.bind(str(replacement))
            replacement.chmod(0o600)
            replacement.replace(inbox.path)
    assert bridge.handle(request(inbox), inbox.record_path)["error"] == "stale_target"


@pytest.mark.parametrize("owner_check", ["record", "socket"])
def test_owner_mismatch_is_rejected(inbox, monkeypatch, owner_check):
    if owner_check == "record":
        real = bridge.os.fstat
        def wrong_owner(fd):
            info = real(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
        monkeypatch.setattr(bridge.os, "fstat", wrong_owner)
    else:
        real = bridge.os.stat
        def wrong_owner(path, **kwargs):
            info = real(path, **kwargs)
            if str(path) == str(inbox.path):
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1,
                                       st_dev=info.st_dev, st_ino=info.st_ino)
            return info
        monkeypatch.setattr(bridge.os, "stat", wrong_owner)
    assert bridge.handle(request(inbox), inbox.record_path)["error"] == "stale_target"


@pytest.mark.parametrize("fields", [
    {"dispatch_id": "not-a-uuid"}, {"text": "missing dispatch"}, {"text": ""},
    {"text": DISPATCH + "x" * 965}, {"text": None}, {"op": "unknown"},
])
def test_invalid_wake_is_rejected_before_connecting(inbox, fields):
    assert bridge.handle(request(inbox, **fields), inbox.record_path)["error"] == "rejected"
    inbox.listener.setblocking(False)
    with pytest.raises(BlockingIOError):
        inbox.listener.accept()


def test_1000_character_wake_is_allowed(inbox):
    text = DISPATCH + "x" * (1000 - len(DISPATCH))
    assert bridge.handle(request(inbox, text=text), inbox.record_path)["error"] == "ambiguous"
    assert read_frame(inbox)["message"]["content"] == text


def test_target_is_rechecked_after_connect_before_writing(inbox, monkeypatch):
    class ChangedConnection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect(self, path):
            inbox.record["session_id"] = "33333333-3333-4333-8333-333333333333"
            inbox.record_path.write_text(json.dumps(inbox.record))
        def sendall(self, data):
            pytest.fail("A changed target must not receive a frame")
    monkeypatch.setattr(bridge.socket, "socket", lambda *args: ChangedConnection())
    assert bridge.handle(request(inbox), inbox.record_path)["error"] == "stale_target"


@pytest.mark.parametrize("reuse_path", [False, True])
def test_transport_change_during_connect_does_not_write_to_either_inbox(inbox, monkeypatch, reuse_path):
    resumed_path = inbox.directory / "resumed.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as resumed:
        resumed.bind(str(resumed_path))
        resumed_path.chmod(0o600)
        resumed.listen(4)
        resumed.setblocking(False)
        class ChangedConnection:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): pass
            def connect(self, path):
                assert path == str(inbox.path)
                current_path = resumed_path
                if reuse_path:
                    resumed_path.replace(inbox.path)
                    current_path = inbox.path
                info = current_path.stat()
                inbox.record.update(socket=str(current_path), socket_dev=info.st_dev, socket_ino=info.st_ino)
                inbox.record_path.write_text(json.dumps(inbox.record))
            def sendall(self, data):
                pytest.fail("A changed address snapshot must not receive a frame")
        monkeypatch.setattr(bridge.socket, "socket", lambda *args: ChangedConnection())
        assert bridge.handle(request(inbox), inbox.record_path)["error"] == "stale_target"
        with pytest.raises(BlockingIOError):
            resumed.accept()


@pytest.mark.parametrize("stage,error,expected", [
    ("connect", ConnectionRefusedError, "unavailable"),
    ("send", BrokenPipeError, "ambiguous"), ("send", TimeoutError, "ambiguous"),
    ("send", RuntimeError, "ambiguous"), ("shutdown", OSError, "ambiguous"),
    ("close", OSError, "ambiguous"),
])
def test_failed_or_partial_writes_are_never_retried(inbox, monkeypatch, stage, error, expected):
    writes = []
    class FailingConnection:
        def __enter__(self): return self
        def __exit__(self, *args):
            if stage == "close": raise error()
        def settimeout(self, timeout): pass
        def connect(self, path):
            if stage == "connect": raise error()
        def sendall(self, data):
            writes.append(data)
            if stage == "send": raise error()
        def shutdown(self, how):
            if stage == "shutdown": raise error()
        def close(self): pass
    monkeypatch.setattr(bridge.socket, "socket", lambda *args: FailingConnection())
    result = bridge.handle(request(inbox), inbox.record_path)
    assert result["error"] == expected and result.get("retryable", False) is False
    assert len(writes) == (0 if stage == "connect" else 1)


# --- pinned sessions: resolved through Claude Code's registry of running sessions ------------
@pytest.fixture
def registry(inbox, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    def add(pid=os.getpid(), session=SESSION, status="idle", socket_path=None):
        path = sessions / f"{pid}.json"
        path.write_text(json.dumps({"pid": pid, "sessionId": session, "cwd": "/work", "status": status,
                                    "messagingSocketPath": str(socket_path or inbox.path)}))
        return path

    return SimpleNamespace(dir=sessions, add=add, inbox=inbox, target={"session": "claude:" + SESSION})


def pinned_request(registry, op="send"):
    return {"op": op, "target": registry.target, "dispatch_id": DISPATCH,
            "text": f"[local-ai-connector wake {DISPATCH}] Retrieve the approved message."}


@pytest.mark.parametrize("status,expected", [("idle", "idle"), ("busy", "busy"), ("starting", "unknown")])
def test_pinned_session_confirms_and_reports_registry_status(registry, status, expected):
    registry.add(status=status)
    assert bridge.handle(pinned_request(registry, "confirm"), sessions_dir=registry.dir) == {"ok": True, "target": registry.target}
    assert bridge.handle(pinned_request(registry, "status"), sessions_dir=registry.dir) == {"ok": True, "state": expected}


def test_pinned_session_send_writes_one_frame_for_that_session(registry):
    registry.add()
    reply = bridge.handle(pinned_request(registry), sessions_dir=registry.dir)
    assert reply["ok"] is False and reply["error"] == "ambiguous"  # the inbox never acknowledges
    frame = read_frame(registry.inbox)
    assert frame["session_id"] == SESSION and frame["msg_id"] == DISPATCH and frame["type"] == "user"
    assert DISPATCH in frame["message"]["content"]


def test_pinned_session_that_is_not_running_or_ambiguous_is_unavailable(registry):
    assert bridge.handle(pinned_request(registry, "status"), sessions_dir=registry.dir)["error"] == "unavailable"
    registry.add(pid=99999999)  # the registry outlived its process
    assert bridge.handle(pinned_request(registry, "status"), sessions_dir=registry.dir)["error"] == "unavailable"
    registry.add()
    registry.add(pid=os.getppid())  # two running processes claim the session
    reply = bridge.handle(pinned_request(registry), sessions_dir=registry.dir)
    assert reply["error"] == "unavailable" and "More than one" in reply["detail"]


def test_pinned_session_ignores_other_sessions_and_unsafe_inboxes(registry):
    registry.add(session="33333333-3333-4333-8333-333333333333")
    registry.inbox.path.chmod(0o666)
    registry.add(pid=os.getppid())
    assert bridge.handle(pinned_request(registry, "status"), sessions_dir=registry.dir)["error"] == "unavailable"


@pytest.mark.parametrize("target", [{"session": "codex:" + SESSION}, {"session": "claude:not-a-uuid"},
                                    {"session": "claude:" + SESSION, "workspace": "/work"}])
def test_pinned_session_must_be_one_exact_claude_session(registry, target):
    registry.add()
    reply = bridge.handle({**pinned_request(registry, "confirm"), "target": target}, sessions_dir=registry.dir)
    assert reply == {"ok": False, "error": "rejected", "detail": "Invalid bridge request"}


def test_pinned_inbox_replaced_while_connecting_is_never_written(registry, monkeypatch):
    registry.add()
    original = bridge.pinned_session
    calls = []

    def moved(target, sessions_dir):
        snapshot = original(target, sessions_dir)
        calls.append(1)
        return snapshot if len(calls) == 1 else {**snapshot, "identity": snapshot["identity"] + ("replaced",)}
    monkeypatch.setattr(bridge, "pinned_session", moved)
    assert bridge.handle(pinned_request(registry), sessions_dir=registry.dir)["error"] == "stale_target"


def test_pinned_session_registry_is_read_as_utf8(registry):
    path = registry.add()
    record = json.loads(path.read_text(encoding="utf-8"))
    record["cwd"] = "/Users/张伟/Ünïcode"
    path.write_bytes(json.dumps(record, ensure_ascii=False).encode("utf-8"))
    assert bridge.handle(pinned_request(registry, "status"), sessions_dir=registry.dir) == {"ok": True, "state": "idle"}


def test_pinned_session_whose_pid_was_reused_is_skipped(registry):
    import time
    from local_ai_connector import os_adapter
    path = registry.add()
    record = json.loads(path.read_text())
    record["procStart"] = "Mon Jan  1 00:00:00 2024"  # a different process held this PID
    path.write_text(json.dumps(record))
    assert bridge.handle(pinned_request(registry, "status"), sessions_dir=registry.dir)["error"] == "unavailable"
    record["procStart"] = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(os_adapter.process_start_time(os.getpid())))
    path.write_text(json.dumps(record))
    assert bridge.handle(pinned_request(registry, "status"), sessions_dir=registry.dir)["state"] == "idle"


def test_unreadable_session_records_are_reported(registry):
    (registry.dir / "123.json").write_bytes(b"\xff\xfe not json")
    reply = bridge.handle(pinned_request(registry, "status"), sessions_dir=registry.dir)
    assert reply["error"] == "unavailable" and "1 session record(s) could not be read" in reply["detail"]


# --- the Windows path, with a fake named pipe at the OS-adapter boundary ----------------------
TOKEN = "per-session-token"


class FakePipe:
    def __init__(self):
        self.written = b""

    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def sendall(self, data): self.written += data
    def shutdown(self, how): pass


@pytest.fixture
def windows(registry, monkeypatch):
    from local_ai_connector import os_adapter
    state = SimpleNamespace(connections=[], served_by=os.getpid())

    def connect(path, timeout):
        pipe = FakePipe()
        state.connections.append((path, pipe))
        return pipe
    monkeypatch.setattr(os_adapter, "NAME", "windows")
    monkeypatch.setattr(os_adapter, "connect_local_ipc", connect)
    monkeypatch.setattr(os_adapter, "endpoint_identity", lambda path, connection=None: (
        "pipe", state.served_by, os_adapter.process_start_time(os.getpid())))
    registry.add()
    state.registry = registry
    return state


def windows_send(state, credential=None):
    request = pinned_request(state.registry)
    if credential is not None:
        request["credential"] = credential
    return bridge.handle(request, sessions_dir=state.registry.dir)


def test_windows_status_comes_from_the_registry_without_connecting(windows):
    assert bridge.handle(pinned_request(windows.registry, "status"), sessions_dir=windows.registry.dir) == \
        {"ok": True, "state": "idle"}
    assert windows.connections == []


def test_windows_send_needs_the_chats_token_before_connecting(windows):
    reply = windows_send(windows)
    assert reply["error"] == "unavailable" and "token" in reply["detail"] and windows.connections == []


def test_windows_send_authenticates_first_then_delivers_one_frame(windows):
    reply = windows_send(windows, {"token": TOKEN, "inbox": str(windows.registry.inbox.path)})
    assert reply["error"] == "ambiguous" and TOKEN not in json.dumps(reply)
    [(path, pipe)] = windows.connections
    auth, frame, rest = pipe.written.split(b"\n")
    assert json.loads(auth) == {"type": "auth", "token": TOKEN} and rest == b""
    assert json.loads(frame)["session_id"] == SESSION and json.loads(frame)["msg_id"] == DISPATCH


def test_windows_send_refuses_another_server_or_a_different_inbox(windows):
    windows.served_by = os.getppid()
    assert windows_send(windows, {"token": TOKEN, "inbox": None})["error"] == "stale_target"
    assert windows.connections[-1][1].written == b""  # nothing was written to the wrong server
    windows.served_by = os.getpid()
    reply = windows_send(windows, {"token": TOKEN, "inbox": r"\\.\pipe\LOCAL\cc-msg-other"})
    assert reply["error"] == "stale_target" and len(windows.connections) == 1  # refused before connecting


def test_windows_refuses_captured_session_targets_clearly(windows, inbox):
    reply = bridge.handle(request(inbox), inbox.record_path)
    assert reply == {"ok": False, "error": "rejected",
                     "detail": "Captured-session targets work on macOS and Linux only; bind the chat instead"}


def test_macos_and_linux_never_send_the_token(registry):
    registry.add()
    request = {**pinned_request(registry), "credential": {"token": TOKEN, "inbox": None}}
    reply = bridge.handle(request, sessions_dir=registry.dir)
    assert reply["error"] == "ambiguous"
    frame = read_frame(registry.inbox)  # exactly one line: the wake frame, no auth line
    assert frame["type"] == "user" and TOKEN not in json.dumps(frame)
