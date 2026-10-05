import importlib.util
import json
from pathlib import Path
import socket
import sys

import pytest

PROBE_DIR = Path(__file__).resolve().parents[1] / "integrations/codex_desktop"
sys.path.insert(0, str(PROBE_DIR))
import probe as base_probe

STATUS_PATH = PROBE_DIR / "status_probe.py"
spec = importlib.util.spec_from_file_location("codex_desktop_status_probe", STATUS_PATH)
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)
THREAD_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def framed(message):
    payload = json.dumps(message, separators=(",", ":")).encode()
    return len(payload).to_bytes(4, "little") + payload


class FakeConnection:
    def __init__(self, snapshot=None, *, owner="owner-id", timeout=False, bad_snapshot=False,
                 timeout_method=None, write_timeout_method=None, discovery_error=None,
                 partial_method=None, partial_frame=None):
        self.snapshot = snapshot
        self.owner = owner
        self.timeout = timeout
        self.bad_snapshot = bad_snapshot
        self.timeout_method = timeout_method
        self.write_timeout_method = write_timeout_method
        self.discovery_error = discovery_error
        self.partial_method = partial_method
        self.partial_frame = partial_frame
        self.buffer = bytearray()
        self.requests = []
        self.broadcasts = []
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def connect(self, path):
        self.path = path

    def sendall(self, frame):
        size = int.from_bytes(frame[:4], "little")
        message = json.loads(frame[4:4 + size])
        if message["type"] == "broadcast":
            self.broadcasts.append(message)
            if message["params"]["following"] and not self.timeout:
                if self.bad_snapshot:
                    self.buffer.extend(b"\x01\x00\x00\x00?")
                else:
                    snapshot = self.snapshot or {
                        "type": "broadcast", "method": "thread-stream-state-changed",
                        "sourceClientId": self.owner, "version": status.SNAPSHOT_VERSION,
                        "params": {"conversationId": THREAD_ID, "hostId": "local",
                                   "change": {"type": "snapshot", "revision": 8,
                                              "conversationState": {"id": THREAD_ID, "cwd": "/safe/path",
                                                  "threadRuntimeStatus": {"type": "active"},
                                                  "turns": [{"turnId": "turn-id", "status": "completed",
                                                             "isSleeping": False}]}}},
                    }
                    self.buffer.extend(framed(snapshot))
            return

        self.requests.append(message)
        if message["method"] == self.write_timeout_method:
            raise socket.timeout()
        if message["method"] == self.timeout_method:
            return
        response = {"type": "response", "requestId": message["requestId"],
                    "method": message["method"], "resultType": "success"}
        if message["method"] == "initialize":
            response["result"] = {"clientId": "probe-client"}
        elif message["method"] == "thread-owner-discovery":
            if self.discovery_error:
                response.update(resultType="error", error=self.discovery_error)
            else:
                response["handledByClientId"] = self.owner
        encoded = framed(response)
        if message['method'] == self.partial_method:
            self.buffer.extend(encoded[:2] if self.partial_frame == 'header'
                               else encoded[:6])
        else:
            self.buffer.extend(encoded)

    def recv(self, size):
        if not self.buffer:
            raise socket.timeout()
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def close(self):
        self.closed = True


def setup(monkeypatch, tmp_path, connection):
    socket_path = tmp_path / "ipc.sock"
    monkeypatch.setattr(status.base, "validate_target", lambda _path: (1, 2))
    monkeypatch.setattr(status.socket, "socket", lambda *_args: connection)
    return socket_path


def test_probe_subscribes_to_owner_snapshot_and_unsubscribes(monkeypatch, tmp_path):
    connection = FakeConnection()
    socket_path = setup(monkeypatch, tmp_path, connection)

    result = status.probe(THREAD_ID, socket_path)

    assert result == {
        "thread_id": THREAD_ID, "owner_found": True, "snapshot_revision": 8,
        "conversation_id": THREAD_ID, "latest_turn_id": "turn-id",
        "latest_turn_status": "completed", "latest_turn_sleeping": False,
        "runtime_status": "active", "cwd": "/safe/path",
    }
    assert [request["method"] for request in connection.requests] == [
        "initialize", "thread-owner-discovery",
    ]
    assert connection.broadcasts[0] == {
        "type": "broadcast", "method": "thread-stream-following-changed",
        "sourceClientId": "probe-client", "targetClientIds": ["owner-id"],
        "version": 1,
        "params": {"conversationId": THREAD_ID, "hostId": "local", "following": True},
    }
    assert connection.broadcasts[-1]["params"]["following"] is False
    assert connection.broadcasts[-1]["targetClientIds"] == ["owner-id"]
    assert connection.closed


def test_probe_does_not_infer_status_from_unknown_snapshot_schema(monkeypatch, tmp_path):
    connection = FakeConnection(snapshot={
        "type": "broadcast", "method": "thread-stream-state-changed",
        "sourceClientId": "owner-id", "version": status.SNAPSHOT_VERSION,
        "params": {"conversationId": THREAD_ID, "hostId": "local", "change": {
            "type": "snapshot", "revision": 2, "conversationState": {
                "id": THREAD_ID, "historyMode": "paginated", "turns": [],
                "threadRuntimeStatus": {"type": "unrecognized-private-value"}},
        }},
    })
    socket_path = setup(monkeypatch, tmp_path, connection)

    result = status.probe(THREAD_ID, socket_path)

    assert result["latest_turn_status"] == "unknown"
    assert result["latest_turn_id"] is None
    assert result["latest_turn_sleeping"] is None
    assert result["runtime_status"] == "unknown"
    assert "historyMode" not in result


def test_paginated_empty_history_uses_current_runtime_status(monkeypatch, tmp_path):
    connection = FakeConnection(snapshot={
        "type": "broadcast", "method": "thread-stream-state-changed",
        "sourceClientId": "owner-id", "version": status.SNAPSHOT_VERSION,
        "params": {"conversationId": THREAD_ID, "hostId": "local", "change": {
            "type": "snapshot", "revision": 2, "conversationState": {
                "id": THREAD_ID, "historyMode": "paginated", "turns": [],
                "threadRuntimeStatus": {"type": "idle"}},
        }},
    })
    socket_path = setup(monkeypatch, tmp_path, connection)

    result = status.probe(THREAD_ID, socket_path)

    assert result["runtime_status"] == "idle"
    assert result["latest_turn_status"] == "unknown"
    assert result["latest_turn_id"] is None


def test_probe_rejects_snapshot_from_wrong_owner_and_unsubscribes(monkeypatch, tmp_path):
    connection = FakeConnection(snapshot={
        "type": "broadcast", "method": "thread-stream-state-changed",
        "sourceClientId": "other-owner", "version": status.SNAPSHOT_VERSION,
        "params": {"conversationId": THREAD_ID, "hostId": "local",
                   "change": {"type": "snapshot", "revision": 1,
                              "conversationState": {"id": THREAD_ID}}},
    })
    socket_path = setup(monkeypatch, tmp_path, connection)

    with pytest.raises(base_probe.ProbeError, match="unexpected owner"):
        status.probe(THREAD_ID, socket_path)

    assert connection.broadcasts[-1]["params"]["following"] is False
    assert connection.closed


def test_probe_ignores_other_thread_broadcast_before_matching_snapshot(monkeypatch, tmp_path):
    other = {"type": "broadcast", "method": "thread-stream-state-changed",
             "sourceClientId": "owner-id", "version": status.SNAPSHOT_VERSION,
             "params": {"conversationId": "other-thread", "hostId": "local",
                        "change": {"type": "snapshot", "revision": 4,
                                   "conversationState": {"id": "other-thread"}}}}
    matching = {"type": "broadcast", "method": "thread-stream-state-changed",
                "sourceClientId": "owner-id", "version": status.SNAPSHOT_VERSION,
                "params": {"conversationId": THREAD_ID, "hostId": "local",
                           "change": {"type": "snapshot", "revision": 5,
                                      "conversationState": {"id": THREAD_ID}}}}
    connection = FakeConnection(snapshot=matching)
    connection.snapshot_prefix = other
    original_sendall = connection.sendall

    def sendall(frame):
        size = int.from_bytes(frame[:4], "little")
        message = json.loads(frame[4:4 + size])
        if message["type"] == "broadcast" and message["params"]["following"]:
            connection.buffer.extend(framed(other))
        original_sendall(frame)

    connection.sendall = sendall
    socket_path = setup(monkeypatch, tmp_path, connection)

    assert status.probe(THREAD_ID, socket_path)["snapshot_revision"] == 5


@pytest.mark.parametrize("change, message", [
    ({"version": status.SNAPSHOT_VERSION + 1}, "version did not match"),
    ({"hostId": "remote"}, "host did not match"),
    ({"inner_id": "other-thread"}, "thread identity did not match"),
])
def test_probe_rejects_snapshot_identity_mismatches(monkeypatch, tmp_path, change, message):
    snapshot = {"type": "broadcast", "method": "thread-stream-state-changed",
                "sourceClientId": "owner-id", "version": status.SNAPSHOT_VERSION,
                "params": {"conversationId": THREAD_ID, "hostId": "local",
                           "change": {"type": "snapshot", "revision": 3,
                                      "conversationState": {"id": THREAD_ID}}}}
    if "version" in change:
        snapshot.update(change)
    elif "inner_id" in change:
        snapshot["params"]["change"]["conversationState"]["id"] = change["inner_id"]
    else:
        snapshot["params"].update(change)
    connection = FakeConnection(snapshot=snapshot)
    socket_path = setup(monkeypatch, tmp_path, connection)

    with pytest.raises(base_probe.ProbeError, match=message):
        status.probe(THREAD_ID, socket_path)

    assert connection.broadcasts[-1]["params"]["following"] is False
    assert connection.closed


@pytest.mark.parametrize("failure, message", [
    ("timeout", "timed out"), ("malformed", "malformed JSON"),
])
def test_probe_failure_still_unsubscribes_and_closes(monkeypatch, tmp_path, failure, message):
    connection = FakeConnection(timeout=failure == "timeout", bad_snapshot=failure == "malformed")
    socket_path = setup(monkeypatch, tmp_path, connection)

    with pytest.raises(base_probe.ProbeError, match=message) as error:
        status.probe(THREAD_ID, socket_path)
    if failure == "timeout":
        assert error.value.kind == "unavailable"
        assert isinstance(error.value, base_probe.ProbeTimeout)

    assert connection.broadcasts[-1]["params"]["following"] is False
    assert connection.closed


@pytest.mark.parametrize(('timeout_method', 'expected_kind'), [
    ('initialize', 'unavailable'),
    ('thread-owner-discovery', 'owner_discovery_timeout'),
])
def test_owner_discovery_response_timeout_has_a_distinct_recovery_kind(
        monkeypatch, tmp_path, timeout_method, expected_kind):
    connection = FakeConnection(timeout_method=timeout_method)
    socket_path = setup(monkeypatch, tmp_path, connection)

    with pytest.raises(base_probe.ProbeError) as error:
        status.probe(THREAD_ID, socket_path)

    assert error.value.kind == expected_kind
    if timeout_method == 'initialize':
        assert isinstance(error.value, base_probe.ProbeTimeout)
    assert connection.requests[-1]['method'] == timeout_method
    assert connection.broadcasts == []
    assert connection.closed


def test_owner_discovery_write_timeout_stays_generic(monkeypatch, tmp_path):
    connection = FakeConnection(write_timeout_method='thread-owner-discovery')
    socket_path = setup(monkeypatch, tmp_path, connection)

    with pytest.raises(base_probe.ProbeError) as error:
        status.probe(THREAD_ID, socket_path)

    assert error.value.kind == 'unavailable'
    assert connection.requests[-1]['method'] == 'thread-owner-discovery'
    assert connection.broadcasts == []
    assert connection.closed


@pytest.mark.parametrize('method,expected_kind', [
    ('initialize', 'unavailable'),
    ('thread-owner-discovery', 'owner_discovery_timeout'),
])
@pytest.mark.parametrize('partial_frame', ['header', 'payload'])
def test_partial_response_timeout_keeps_phase_specific_classification(
        monkeypatch, tmp_path, method, expected_kind, partial_frame):
    connection = FakeConnection(partial_method=method, partial_frame=partial_frame)
    socket_path = setup(monkeypatch, tmp_path, connection)

    with pytest.raises(base_probe.ProbeError) as error:
        status.probe(THREAD_ID, socket_path)

    assert error.value.kind == expected_kind
    if method == 'initialize':
        assert isinstance(error.value, base_probe.ProbeTimeout)
    assert connection.requests[-1]['method'] == method
    assert connection.broadcasts == []
    assert connection.closed


@pytest.mark.parametrize('native_error', ['no-client-found', 'no-handler-for-request'])
def test_native_explicit_owner_absence_keeps_no_owner_classification(
        monkeypatch, tmp_path, native_error):
    connection = FakeConnection(discovery_error=native_error)
    socket_path = setup(monkeypatch, tmp_path, connection)

    with pytest.raises(base_probe.ProbeError) as error:
        status.probe(THREAD_ID, socket_path)

    assert error.value.kind == 'no_owner'
    assert connection.broadcasts == []
    assert connection.closed


def test_probe_fails_closed_when_owner_is_missing(monkeypatch, tmp_path):
    connection = FakeConnection(owner=None)
    socket_path = setup(monkeypatch, tmp_path, connection)

    with pytest.raises(base_probe.ProbeError, match="no owner"):
        status.probe(THREAD_ID, socket_path)

    assert not connection.broadcasts
    assert connection.closed
