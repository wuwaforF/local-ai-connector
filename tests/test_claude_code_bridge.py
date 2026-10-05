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

pytestmark = pytest.mark.posix  # Unix sockets, owner/mode bits or fcntl


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
    monkeypatch.setattr(bridge.socket, "socket", lambda *args: FailingConnection())
    result = bridge.handle(request(inbox), inbox.record_path)
    assert result["error"] == expected and result.get("retryable", False) is False
    assert len(writes) == (0 if stage == "connect" else 1)
