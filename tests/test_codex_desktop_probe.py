import importlib.util
import json
from pathlib import Path
import socket
import stat
from types import SimpleNamespace
from uuid import UUID

import pytest

pytestmark = pytest.mark.posix  # Unix sockets, owner/mode bits or fcntl


PROBE_PATH = Path(__file__).resolve().parents[1] / "integrations/codex_desktop/probe.py"
spec = importlib.util.spec_from_file_location("codex_desktop_probe", PROBE_PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
THREAD_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def framed(message):
    payload = json.dumps(message, separators=(",", ":")).encode()
    return len(payload).to_bytes(4, "little") + payload


class FakeConnection:
    def __init__(self, replies):
        self.replies = replies
        self.buffer = bytearray()
        self.requests = []
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def connect(self, path):
        self.path = path

    def sendall(self, data):
        size = int.from_bytes(data[:4], "little")
        request = json.loads(data[4:4 + size])
        self.requests.append(request)
        self.buffer.extend(self.replies(request))

    def recv(self, size):
        if not self.buffer:
            raise socket.timeout()
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def close(self):
        self.closed = True


def private_socket_stat(mode=0o600):
    return SimpleNamespace(st_mode=stat.S_IFSOCK | mode, st_uid=probe.os.getuid(), st_dev=7, st_ino=9)


def install_fake_socket(monkeypatch, socket_path, connection):
    real_lstat = probe.os.lstat

    def lstat(path):
        if Path(path) == socket_path:
            return private_socket_stat()
        return real_lstat(path)

    monkeypatch.setattr(probe.os, "lstat", lstat)
    monkeypatch.setattr(probe.socket, "socket", lambda *_args: connection)


def normal_replies(request):
    response = {"type": "response", "requestId": request["requestId"],
                "method": request["method"], "resultType": "success"}
    if request["method"] == "initialize":
        response["result"] = {"clientId": "private-test-client"}
        return framed({"type": "broadcast", "method": "query-cache-invalidate"}) + framed(response)
    response["handledByClientId"] = "desktop-owner-secret"
    return framed(response)


def test_probe_only_initializes_and_discovers_owner(monkeypatch, tmp_path):
    socket_path = tmp_path / "ipc.sock"
    connection = FakeConnection(normal_replies)
    install_fake_socket(monkeypatch, socket_path, connection)

    assert probe.probe(THREAD_ID, socket_path) == {
        "ok": True, "thread_id": THREAD_ID, "owner_found": True,
    }
    assert [request["method"] for request in connection.requests] == [
        "initialize", "thread-owner-discovery",
    ]
    assert connection.requests[0]["params"] == {"clientType": "local-ai-connector-probe"}
    assert connection.requests[1]["params"] == {
        "hostId": "local", "conversationId": THREAD_ID,
    }
    assert connection.requests[1]["sourceClientId"] == "private-test-client"
    assert all(request["method"] not in {
        "thread-follower-start-turn", "thread/start", "thread-read",
        "thread-follower-load-complete-history", "thread-follower-steer-turn",
        "thread-follower-interrupt-turn", "thread-follower-command-approval-decision",
        "thread-follower-file-approval-decision", "thread-follower-submit-user-input",
    } for request in connection.requests)
    assert connection.closed


def test_absent_owner_fails_without_printing_owner_id(monkeypatch, tmp_path):
    socket_path = tmp_path / "ipc.sock"

    def replies(request):
        response = {"type": "response", "requestId": request["requestId"],
                    "resultType": "success"}
        if request["method"] == "initialize":
            response["result"] = {"clientId": "test-client"}
        return framed(response)

    connection = FakeConnection(replies)
    install_fake_socket(monkeypatch, socket_path, connection)
    with pytest.raises(probe.ProbeError, match="no owner"):
        probe.probe(THREAD_ID, socket_path)


@pytest.mark.parametrize("reply, message", [
    (lambda _request: b"\x01\x00\x00\x00?", "malformed JSON"),
    (lambda _request: b"", "timed out"),
])
def test_malformed_frame_and_timeout_fail(monkeypatch, tmp_path, reply, message):
    socket_path = tmp_path / "ipc.sock"
    connection = FakeConnection(reply)
    install_fake_socket(monkeypatch, socket_path, connection)
    with pytest.raises(probe.ProbeError, match=message):
        probe.probe(THREAD_ID, socket_path)


def test_unknown_message_type_is_reported_without_echoing_payload(monkeypatch, tmp_path):
    socket_path = tmp_path / "ipc.sock"
    connection = FakeConnection(lambda _request: framed({"type": [], "payload": "private"}))
    install_fake_socket(monkeypatch, socket_path, connection)
    with pytest.raises(probe.ProbeError, match="unexpected message type") as error:
        probe.probe(THREAD_ID, socket_path)
    assert "[]" in str(error.value)
    assert "private" not in str(error.value)


def test_private_socket_mode_is_required_and_connect_is_never_attempted(monkeypatch, tmp_path):
    socket_path = tmp_path / "ipc.sock"
    real_lstat = probe.os.lstat
    monkeypatch.setattr(probe.os, "lstat", lambda path: (
        private_socket_stat(0o660) if Path(path) == socket_path else real_lstat(path)
    ))
    connected = False

    class NeverConnect:
        def connect(self, _path):
            nonlocal connected
            connected = True

    monkeypatch.setattr(probe.socket, "socket", lambda *_args: NeverConnect())
    with pytest.raises(probe.ProbeError, match="private socket"):
        probe.probe(THREAD_ID, socket_path)
    assert not connected


def test_thread_id_must_be_canonical_uuid(tmp_path):
    with pytest.raises(probe.ProbeError, match="canonical UUID"):
        probe.probe(str(UUID(THREAD_ID)).upper(), tmp_path / "ipc.sock")


def test_missing_socket_is_recoverable_but_invalid_parent_is_not(tmp_path):
    with pytest.raises(probe.ProbeError) as missing:
        probe.validate_target(tmp_path / 'missing.sock')
    assert missing.value.kind == 'socket_missing'
    unsafe = tmp_path / 'unsafe'
    unsafe.mkdir(mode=0o755)
    with pytest.raises(probe.ProbeError) as invalid:
        probe.validate_target(unsafe / 'missing.sock')
    assert invalid.value.kind == 'invalid_socket'


@pytest.mark.parametrize('error, expected', [
    ('no-client-found', 'no_owner'),
    ('no-handler-for-request', 'no_owner'), ('unsupported-version', 'protocol'),
])
def test_owner_discovery_error_distinguishes_absence_and_protocol(monkeypatch, tmp_path, error, expected):
    def replies(request):
        if request['method'] == 'initialize':
            return normal_replies(request)
        return framed({'type': 'response', 'requestId': request['requestId'],
                       'resultType': 'error', 'error': error})
    connection = FakeConnection(replies)
    install_fake_socket(monkeypatch, tmp_path / 'ipc.sock', connection)
    with pytest.raises(probe.ProbeError) as failure:
        probe.probe(THREAD_ID, tmp_path / 'ipc.sock')
    assert failure.value.kind == expected
