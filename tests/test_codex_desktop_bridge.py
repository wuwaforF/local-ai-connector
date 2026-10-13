import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import sys

import pytest

PROBE_DIR = Path(__file__).resolve().parents[1] / "integrations/codex_desktop"
sys.path.insert(0, str(PROBE_DIR))
import probe as base_probe

BRIDGE_PATH = PROBE_DIR / "bridge.py"
spec = importlib.util.spec_from_file_location("codex_desktop_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

THREAD_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
DISPATCH_ID = "12345678-1234-4234-8234-123456789abc"
TARGET = {"thread_id": THREAD_ID, "workspace": "/workspace/project"}
WAKE = bridge.WAKE_TEXT.format(dispatch=DISPATCH_ID, after=0)


@pytest.fixture(autouse=True)
def empty_codex_home(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(bridge, "DEFAULT_CODEX_HOME", tmp_path_factory.mktemp("codex-home"))


def codex_store(home, *, archived, version=5):
    with sqlite3.connect(home / f"state_{version}.sqlite") as db:
        db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, archived INTEGER NOT NULL)")
        db.execute("INSERT OR REPLACE INTO threads VALUES (?, ?)", (THREAD_ID, int(archived)))
    db.close()


def snapshot(runtime="idle", *, cwd="/workspace/project", extra=None):
    conversation = {"id": THREAD_ID, "cwd": cwd, "threadRuntimeStatus": {"type": runtime},
                    "turns": [], "requests": []}
    if extra:
        conversation.update(extra)
    return {"type": "broadcast", "method": "thread-stream-state-changed",
            "sourceClientId": "desktop-owner", "version": bridge.status_probe.SNAPSHOT_VERSION,
            "params": {"conversationId": THREAD_ID, "hostId": "local",
                       "change": {"type": "snapshot", "revision": 9,
                                  "conversationState": conversation}}}


class FakeConnection:
    def __init__(self, *, timeout_start=False, wrong_owner=False, missing_turn=False):
        self.requests = []
        self.timeout_start = timeout_start
        self.wrong_owner = wrong_owner
        self.missing_turn = missing_turn

    def settimeout(self, _timeout):
        pass

    def sendall(self, frame):
        size = int.from_bytes(frame[:4], "little")
        request = json.loads(frame[4:4 + size])
        self.requests.append(request)
        if request["method"] == "thread-follower-start-turn" and self.timeout_start:
            return
        response = {"type": "response", "requestId": request["requestId"],
                    "method": request["method"], "resultType": "success",
                    "handledByClientId": "wrong-owner" if self.wrong_owner else "desktop-owner"}
        if not self.missing_turn:
            response["result"] = {"result": {"turn": {"id": "turn-id"}}}
        self.pending = bytearray(len(json.dumps(response, separators=(",", ":")).encode()).to_bytes(4, "little")
                                 + json.dumps(response, separators=(",", ":")).encode())

    def recv(self, size):
        if not getattr(self, "pending", None):
            raise socket.timeout()
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result


def patch_snapshot(monkeypatch, live_snapshot, connection):
    def with_snapshot(thread_id, _socket_path, consume):
        assert thread_id == THREAD_ID
        return consume(connection, "probe-client", "desktop-owner", live_snapshot)
    monkeypatch.setattr(bridge.status_probe, "with_snapshot", with_snapshot)


def test_confirm_and_status_require_exact_thread_workspace(monkeypatch, tmp_path):
    connection = FakeConnection()
    patch_snapshot(monkeypatch, snapshot(), connection)

    assert bridge.handle({"op": "confirm", "target": TARGET}) == {"ok": True, "target": TARGET}
    assert bridge.handle({"op": "status", "target": TARGET}) == {"ok": True, "state": "idle"}

    patch_snapshot(monkeypatch, snapshot(cwd="/workspace/other"), connection)
    assert bridge.handle({"op": "confirm", "target": TARGET})["error"] == "stale_target"


@pytest.mark.parametrize("runtime, expected", [
    ("idle", "idle"), ("active", "busy"), ("systemError", "unknown"),
    ("future-state", "unknown"),
])
def test_status_maps_only_verified_runtime_states(monkeypatch, tmp_path, runtime, expected):
    patch_snapshot(monkeypatch, snapshot(runtime), FakeConnection())
    assert bridge.handle({"op": "status", "target": TARGET}) == {"ok": True, "state": expected}


def test_pending_or_queued_state_never_reports_idle(monkeypatch, tmp_path):
    patch_snapshot(monkeypatch, snapshot(extra={"requests": [{"method": "permissions/request"}]}),
                   FakeConnection())
    assert bridge.handle({"op": "status", "target": TARGET}) == {"ok": True, "state": "unknown"}


def test_send_records_intent_before_exactly_one_start_and_ack(monkeypatch, tmp_path):
    connection = FakeConnection()
    patch_snapshot(monkeypatch, snapshot(), connection)
    request = {"op": "send", "target": TARGET, "dispatch_id": DISPATCH_ID, "text": WAKE}

    result = bridge.handle(request, state_dir=tmp_path / "state")

    assert result == {"ok": True, "accepted": True, "host_ref": "turn-id"}
    assert len(connection.requests) == 1
    sent = connection.requests[0]
    assert sent["method"] == "thread-follower-start-turn"
    assert sent["version"] == 2 and sent["targetClientId"] == "desktop-owner"
    assert sent["params"]["conversationId"] == THREAD_ID
    assert sent["params"]["turnStart"]["request"]["input"][0]["text"] == WAKE
    record = json.loads((tmp_path / "state" / f"{DISPATCH_ID}.json").read_text())
    assert record == {"dispatch_id": DISPATCH_ID, "target": TARGET,
                      "state": "acknowledged", "turn_id": "turn-id"}

    again = bridge.handle(request, state_dir=tmp_path / "state")
    assert again == result
    assert len(connection.requests) == 1


def test_send_gates_on_fresh_idle_snapshot_before_creating_intent(monkeypatch, tmp_path):
    connection = FakeConnection()
    patch_snapshot(monkeypatch, snapshot("active"), connection)
    result = bridge.handle({"op": "send", "target": TARGET,
                            "dispatch_id": DISPATCH_ID, "text": WAKE}, state_dir=tmp_path / "state")

    assert result == {"ok": False, "error": "rejected",
                      "detail": "Codex Desktop thread is not confirmed idle", "retryable": True}
    assert not (tmp_path / "state" / f"{DISPATCH_ID}.json").exists()
    assert connection.requests == []


def test_busy_snapshot_does_not_consume_dispatch_and_later_idle_can_send(monkeypatch, tmp_path):
    connection = FakeConnection()
    current = {"message": snapshot("active")}

    def with_snapshot(thread_id, _socket_path, consume):
        return consume(connection, "probe-client", "desktop-owner", current["message"])

    monkeypatch.setattr(bridge.status_probe, "with_snapshot", with_snapshot)
    request = {"op": "send", "target": TARGET, "dispatch_id": DISPATCH_ID, "text": WAKE}
    first = bridge.handle(request, state_dir=tmp_path / "state")
    assert first["retryable"] is True
    assert not (tmp_path / "state" / f"{DISPATCH_ID}.json").exists()

    current["message"] = snapshot("idle")
    second = bridge.handle(request, state_dir=tmp_path / "state")
    assert second == {"ok": True, "accepted": True, "host_ref": "turn-id"}
    assert len(connection.requests) == 1


def test_ambiguous_start_keeps_intent_and_duplicate_never_resends(monkeypatch, tmp_path):
    connection = FakeConnection(timeout_start=True)
    patch_snapshot(monkeypatch, snapshot(), connection)
    request = {"op": "send", "target": TARGET, "dispatch_id": DISPATCH_ID, "text": WAKE}
    result = bridge.handle(request, state_dir=tmp_path / "state")
    assert result["error"] == "ambiguous"
    assert json.loads((tmp_path / "state" / f"{DISPATCH_ID}.json").read_text())["state"] == "intent"

    replay = bridge.handle(request, state_dir=tmp_path / "state")
    assert replay == {"ok": False, "error": "ambiguous", "detail": "Dispatch was already attempted"}
    assert len(connection.requests) == 1
    assert bridge.handle({"op": "reconcile", "target": TARGET,
                          "dispatch_id": DISPATCH_ID}, state_dir=tmp_path / "state") == {
        "ok": True, "result": "unknown",
    }


def test_reconcile_ack_reports_delivery_and_absence_stays_unknown(tmp_path):
    assert bridge.handle({"op": "reconcile", "target": TARGET,
                          "dispatch_id": DISPATCH_ID}, state_dir=tmp_path / "state") == {
        "ok": True, "result": "unknown",
    }
    path = tmp_path / "state" / f"{DISPATCH_ID}.json"
    path.write_text(json.dumps({"dispatch_id": DISPATCH_ID, "target": TARGET,
                                "state": "acknowledged", "turn_id": "turn-id"}))
    path.chmod(0o600)
    assert bridge.handle({"op": "reconcile", "target": TARGET,
                          "dispatch_id": DISPATCH_ID}, state_dir=tmp_path / "state") == {
        "ok": True, "result": "delivered",
    }


def test_send_rejects_target_mismatch_and_non_wake_content(monkeypatch, tmp_path):
    connection = FakeConnection()
    patch_snapshot(monkeypatch, snapshot(cwd="/elsewhere"), connection)
    request = {"op": "send", "target": TARGET, "dispatch_id": DISPATCH_ID, "text": WAKE}
    result = bridge.handle(request, state_dir=tmp_path / "state")
    assert result["error"] == "stale_target"
    assert not (tmp_path / "state" / f"{DISPATCH_ID}.json").exists()

    bad = {**request, "text": "the whole approved task body " + DISPATCH_ID}
    assert bridge.handle(bad, state_dir=tmp_path / "state")["error"] == "rejected"


def test_wrong_owner_or_missing_turn_ack_remains_ambiguous(monkeypatch, tmp_path):
    for connection in (FakeConnection(wrong_owner=True), FakeConnection(missing_turn=True)):
        patch_snapshot(monkeypatch, snapshot(), connection)
        request = {"op": "send", "target": TARGET, "dispatch_id": DISPATCH_ID,
                   "text": WAKE}
        result = bridge.handle(request, state_dir=tmp_path / str(id(connection)))
        assert result["error"] == "ambiguous"
        assert json.loads((tmp_path / str(id(connection)) / f"{DISPATCH_ID}.json").read_text())["state"] == "intent"
        assert len(connection.requests) == 1


def test_model_input_cannot_select_state_directory_or_extra_fields():
    result = bridge.handle({"op": "confirm", "target": TARGET,
                            "state_dir": "/tmp/attacker"})
    assert result == {"ok": False, "error": "rejected",
                      "detail": "Unexpected bridge request fields"}


@pytest.mark.parametrize('absence', ['no_owner', 'socket_missing', 'startup', 'owner_discovery_timeout'])
def test_restore_only_navigates_exact_cold_thread(monkeypatch, absence):
    opens = []

    def cold(*_args):
        raise base_probe.ProbeError('synthetic cold target', kind=absence)
    monkeypatch.setattr(bridge.status_probe, 'with_snapshot', cold)

    def opened(argv, **kwargs):
        opens.append(argv)
        assert kwargs['timeout'] == 5 and kwargs['stdin'] is bridge.subprocess.DEVNULL
        return type('Result', (), {'returncode': 0})()
    monkeypatch.setattr(bridge.subprocess, 'run', opened)
    assert bridge.handle({'op': 'restore', 'target': TARGET}) == {'ok': True, 'restored': True}
    assert opens == [['/usr/bin/open', '-b', 'com.openai.codex', 'codex://threads/' + THREAD_ID]]
    for op in ['confirm', 'status']:
        assert bridge.handle({'op': op, 'target': TARGET})['error'] == absence
    assert len(opens) == 1


def test_send_owner_discovery_timeout_is_retryable_only_before_side_effect(monkeypatch, tmp_path):
    def discovery_timeout(*_args):
        raise base_probe.ProbeError('owner discovery timed out', kind='owner_discovery_timeout')
    monkeypatch.setattr(bridge.status_probe, 'with_snapshot', discovery_timeout)
    request = {'op': 'send', 'target': TARGET, 'dispatch_id': DISPATCH_ID, 'text': WAKE}

    result = bridge.handle(request, state_dir=tmp_path / 'state')

    assert result == {'ok': False, 'error': 'owner_discovery_timeout',
                      'detail': 'owner discovery timed out', 'retryable': True}
    assert not (tmp_path / 'state' / f'{DISPATCH_ID}.json').exists()


@pytest.mark.parametrize('runtime', ['idle', 'active', 'systemError', 'future-state'])
def test_restore_does_not_navigate_already_loaded_target(monkeypatch, runtime):
    patch_snapshot(monkeypatch, snapshot(runtime), FakeConnection())
    monkeypatch.setattr(bridge.subprocess, 'run', lambda *_a, **_k: pytest.fail('hot target opened'))
    assert bridge.handle({'op': 'restore', 'target': TARGET}) == {'ok': True, 'restored': False}


def test_restore_does_not_navigate_loaded_workspace_mismatch(monkeypatch):
    patch_snapshot(monkeypatch, snapshot(cwd='/wrong/workspace'), FakeConnection())
    monkeypatch.setattr(bridge.subprocess, 'run', lambda *_a, **_k: pytest.fail('mismatched target opened'))
    assert bridge.handle({'op': 'restore', 'target': TARGET})['error'] == 'stale_target'


@pytest.mark.parametrize('failure', ['permissions', 'invalid_socket', 'protocol', 'unavailable', 'stale_target'])
def test_restore_does_not_navigate_unverified_failures(monkeypatch, failure):
    def failed(*_args):
        raise base_probe.ProbeError('synthetic failure', kind=failure)
    monkeypatch.setattr(bridge.status_probe, 'with_snapshot', failed)
    monkeypatch.setattr(bridge.subprocess, 'run', lambda *_a, **_k: pytest.fail('unverified target opened'))
    assert bridge.handle({'op': 'restore', 'target': TARGET})['error'] == failure


@pytest.mark.parametrize('extra', [{'text': 'override'}, {'cwd': '/other'}, {'model': 'other'}])
def test_restore_rejects_content_or_settings_overrides(monkeypatch, extra):
    monkeypatch.setattr(bridge.subprocess, 'run', lambda *_a, **_k: pytest.fail('invalid restore opened'))
    assert bridge.handle({'op': 'restore', 'target': TARGET, **extra})['error'] == 'rejected'


def test_restore_open_timeout_reports_failure_without_sending(monkeypatch):
    def cold(*_args):
        raise base_probe.ProbeError('cold', kind='no_owner')
    def timeout(*_args, **_kwargs):
        raise bridge.subprocess.TimeoutExpired('/usr/bin/open', 5)
    monkeypatch.setattr(bridge.status_probe, 'with_snapshot', cold)
    monkeypatch.setattr(bridge.subprocess, 'run', timeout)
    assert bridge.handle({'op': 'restore', 'target': TARGET}) == {
        'ok': False, 'error': 'unavailable', 'detail': 'TimeoutExpired',
    }


@pytest.mark.parametrize('op', ['confirm', 'status', 'restore', 'send'])
def test_archived_thread_is_refused_before_ipc_or_navigation(monkeypatch, tmp_path, op):
    codex_store(tmp_path, archived=True)
    monkeypatch.setattr(bridge.status_probe, 'with_snapshot', lambda *_a: pytest.fail('archived thread probed'))
    monkeypatch.setattr(bridge.subprocess, 'run', lambda *_a, **_k: pytest.fail('archived thread opened'))
    request = {'op': op, 'target': TARGET}
    if op == 'send':
        request.update(dispatch_id=DISPATCH_ID, text=WAKE)

    result = bridge.handle(request, state_dir=tmp_path / 'state', codex_home=tmp_path)

    assert result['ok'] is False and result['error'] == 'archived' and 'retryable' not in result
    assert not (tmp_path / 'state' / f'{DISPATCH_ID}.json').exists()


def test_unarchived_thread_goes_on_to_the_ipc_checks(monkeypatch, tmp_path):
    codex_store(tmp_path, archived=False)
    patch_snapshot(monkeypatch, snapshot(), FakeConnection())
    assert bridge.handle({'op': 'status', 'target': TARGET}, codex_home=tmp_path) == {'ok': True, 'state': 'idle'}


def test_archive_lookup_uses_newest_store_that_knows_the_thread(tmp_path):
    assert bridge._archived(THREAD_ID, tmp_path) is False
    (tmp_path / 'state_9.sqlite').write_bytes(b'not a database')
    with sqlite3.connect(tmp_path / 'state_8.sqlite') as db:
        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)')
    db.close()
    assert bridge._archived(THREAD_ID, tmp_path) is False
    codex_store(tmp_path, archived=True, version=5)
    assert bridge._archived(THREAD_ID, tmp_path) is True
    codex_store(tmp_path, archived=False, version=6)
    assert bridge._archived(THREAD_ID, tmp_path) is False
    assert bridge._archived(THREAD_ID, Path('relative-codex-home')) is False


PINNED = {'session': 'codex:' + THREAD_ID}


def test_pinned_session_target_checks_the_thread_and_not_a_workspace(monkeypatch, tmp_path):
    patch_snapshot(monkeypatch, snapshot(cwd='/any/workspace'), FakeConnection())
    assert bridge.handle({'op': 'confirm', 'target': PINNED}) == {'ok': True, 'target': PINNED}
    assert bridge.handle({'op': 'status', 'target': PINNED}) == {'ok': True, 'state': 'idle'}


def test_pinned_session_target_rejects_another_thread(monkeypatch):
    other = dict(snapshot())
    other['params'] = {**other['params'], 'conversationId': 'bbbbbbbb-cdef-4abc-8def-abcdefabcdef'}
    patch_snapshot(monkeypatch, other, FakeConnection())
    assert bridge.handle({'op': 'confirm', 'target': PINNED})['error'] == 'stale_target'


def test_pinned_session_send_records_the_session_and_starts_one_turn(monkeypatch, tmp_path):
    connection = FakeConnection()
    patch_snapshot(monkeypatch, snapshot(), connection)
    request = {'op': 'send', 'target': PINNED, 'dispatch_id': DISPATCH_ID, 'text': WAKE}

    assert bridge.handle(request, state_dir=tmp_path / 'state') == {'ok': True, 'accepted': True, 'host_ref': 'turn-id'}
    record = json.loads((tmp_path / 'state' / f'{DISPATCH_ID}.json').read_text())
    assert record['target'] == PINNED and record['state'] == 'acknowledged'
    [start] = [r for r in connection.requests if r['method'] == 'thread-follower-start-turn']
    assert start['params']['conversationId'] == THREAD_ID
    assert bridge.handle({'op': 'reconcile', 'target': PINNED, 'dispatch_id': DISPATCH_ID},
                         state_dir=tmp_path / 'state') == {'ok': True, 'result': 'delivered'}


def test_pinned_session_restore_opens_the_exact_thread(monkeypatch):
    def cold(*_args):
        raise base_probe.ProbeError('cold', kind='no_owner')
    opens = []
    monkeypatch.setattr(bridge.status_probe, 'with_snapshot', cold)
    monkeypatch.setattr(bridge.subprocess, 'run',
                        lambda argv, **_k: opens.append(argv) or type('R', (), {'returncode': 0})())
    assert bridge.handle({'op': 'restore', 'target': PINNED}) == {'ok': True, 'restored': True}
    assert opens == [['/usr/bin/open', '-b', 'com.openai.codex', 'codex://threads/' + THREAD_ID]]


def test_archived_pinned_session_is_refused(monkeypatch, tmp_path):
    codex_store(tmp_path, archived=True)
    monkeypatch.setattr(bridge.status_probe, 'with_snapshot', lambda *_a: pytest.fail('archived thread probed'))
    assert bridge.handle({'op': 'status', 'target': PINNED}, codex_home=tmp_path)['error'] == 'archived'


@pytest.mark.parametrize('target', [
    {'session': 'antigravity:' + THREAD_ID},
    {'session': 'codex:not-a-uuid'},
    {'session': 'codex:' + THREAD_ID.upper()},
    {'session': 42},
    {'session': 'codex:' + THREAD_ID, 'workspace': '/workspace/project'},
])
def test_pinned_session_target_must_be_one_exact_codex_thread(monkeypatch, target):
    monkeypatch.setattr(bridge.status_probe, 'with_snapshot', lambda *_a: pytest.fail('invalid target probed'))
    assert bridge.handle({'op': 'confirm', 'target': target})['error'] == 'rejected'


def test_start_turn_waits_longer_than_discovery_for_a_resuming_thread(monkeypatch, tmp_path):
    seen = {}
    real = bridge.probe._send_request

    def send_request(connection, **kwargs):
        if kwargs["method"] == "thread-follower-start-turn":
            seen["wait"] = kwargs["deadline"] - bridge.time.monotonic()
        return real(connection, **kwargs)
    monkeypatch.setattr(bridge.probe, "_send_request", send_request)
    patch_snapshot(monkeypatch, snapshot(), FakeConnection())
    request = {"op": "send", "target": TARGET, "dispatch_id": DISPATCH_ID, "text": WAKE}
    assert bridge.handle(request, state_dir=tmp_path / "state")["accepted"] is True
    assert bridge.probe.TOTAL_TIMEOUT_SECONDS < seen["wait"] <= bridge.START_TURN_TIMEOUT_SECONDS < 30
