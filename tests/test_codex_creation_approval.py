import copy
import io
import json
from pathlib import Path
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from integrations.codex_desktop import creation_approval as approval

GRANT_ID = "admin-grant-1"
INPUT = {
    "prompt": "Inspect a supplied fixture.",
    "target": {"type": "project", "projectId": "project-a", "environment": {"type": "local"}},
    "model": "gpt-6-luna",
    "thinking": "low",
}
EVENT = {
    "hook_event_name": "PermissionRequest",
    "session_id": "session-a",
    "cwd": "/workspace/project-a",
    "tool_name": approval.TOOL,
    "tool_input": INPUT,
}


def store_with_grant(path: Path, *, session="session-a", cwd="/workspace/project-a",
                     tool=approval.TOOL, tool_input=INPUT, expires=200.0, consumed=None):
    # Test fixture provisions rows directly; production code has no mint operation.
    db = sqlite3.connect(path)
    db.execute(approval.GRANT_SCHEMA_SQL)
    db.execute(
        "INSERT INTO creation_grants VALUES(?,?,?,?,?,?,?)",
        (GRANT_ID, session, cwd, tool, json.dumps(tool_input), expires, consumed),
    )
    db.commit()
    db.close()


def test_exact_grant_is_committed_then_single_use(tmp_path):
    path = tmp_path / "grants.sqlite"
    store_with_grant(path)
    assert approval.evaluate(EVENT, path, GRANT_ID, now=100) == (True, "grant-consumed")
    assert approval.evaluate(EVENT, path, GRANT_ID, now=100) == (False, "grant-used")
    db = sqlite3.connect(path)
    assert db.execute("SELECT consumed_at FROM creation_grants").fetchone()[0] == 100
    db.close()


@pytest.mark.parametrize("change", [
    lambda args: args["target"].update(projectId="project-b"),
    lambda args: args.update(prompt="different"),
    lambda args: args.update(model="other-model"),
    lambda args: args.update(thinking="high"),
    lambda args: args.update(extra="added"),
    lambda args: args.pop("thinking"),
    lambda args: args.update(prompt=1),
])
def test_any_argument_change_rejects_without_consuming(tmp_path, change):
    path = tmp_path / "grants.sqlite"
    store_with_grant(path)
    altered = copy.deepcopy(EVENT)
    change(altered["tool_input"])
    assert approval.evaluate(altered, path, GRANT_ID, now=100) == (False, "grant-mismatch")
    assert approval.evaluate(EVENT, path, GRANT_ID, now=100) == (True, "grant-consumed")


@pytest.mark.parametrize("field,value", [
    ("session_id", "session-b"), ("cwd", "/workspace/other"),
])
def test_session_and_cwd_are_bound(tmp_path, field, value):
    path = tmp_path / "grants.sqlite"
    store_with_grant(path)
    altered = dict(EVENT, **{field: value})
    assert approval.evaluate(altered, path, GRANT_ID, now=100) == (False, "grant-mismatch")


@pytest.mark.parametrize("field,value", [
    ("hook_event_name", "PreToolUse"), ("tool_name", "mcp__codex_app__fork_thread"),
])
def test_unrelated_event_or_tool_has_no_decision_and_does_not_consume(tmp_path, field, value):
    path = tmp_path / "grants.sqlite"
    store_with_grant(path)
    assert approval.evaluate(dict(EVENT, **{field: value}), path, GRANT_ID) == (None, "unmatched")
    assert approval.evaluate(EVENT, path, GRANT_ID, now=100) == (True, "grant-consumed")


def test_expiry_is_exclusive_at_boundary(tmp_path):
    path = tmp_path / "grants.sqlite"
    store_with_grant(path, expires=100)
    assert approval.evaluate(EVENT, path, GRANT_ID, now=100) == (False, "grant-expired")


def test_expiry_is_rechecked_after_input_comparison(tmp_path, monkeypatch):
    path = tmp_path / "grants.sqlite"
    store_with_grant(path, expires=101)
    clock = [100]
    monkeypatch.setattr(approval.time, "time", lambda: clock[0])
    canonical = approval._canonical

    def advance(value):
        clock[0] = 101
        return canonical(value)

    monkeypatch.setattr(approval, "_canonical", advance)
    assert approval.evaluate(EVENT, path, GRANT_ID) == (False, "grant-expired")


def test_concurrent_calls_consume_at_most_once(tmp_path):
    path = tmp_path / "grants.sqlite"
    store_with_grant(path)
    barrier = threading.Barrier(2)

    def consume():
        barrier.wait()
        return approval.evaluate(EVENT, path, GRANT_ID, now=100)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: consume(), range(2)))
    assert [result[0] for result in outcomes].count(True) == 1
    assert sorted(result[1] for result in outcomes) == ["grant-consumed", "grant-used"]


def test_missing_grant_denies_but_missing_database_is_no_decision(tmp_path):
    path = tmp_path / "grants.sqlite"
    store_with_grant(path)
    assert approval.evaluate(EVENT, path, "missing", now=100) == (False, "grant-missing")
    with pytest.raises(sqlite3.OperationalError):
        approval.evaluate(EVENT, tmp_path / "absent.sqlite", GRANT_ID, now=100)


class Stdin:
    def __init__(self, payload):
        self.buffer = io.BytesIO(payload)


def test_hook_outputs_single_call_allow_and_unrelated_no_decision(tmp_path, monkeypatch, capsys):
    path = tmp_path / "grants.sqlite"
    store_with_grant(path, expires=time.time() + 3600)
    monkeypatch.setattr(approval.sys, "stdin", Stdin(json.dumps(EVENT).encode()))
    assert approval.main(["--store", str(path), "--grant-id", GRANT_ID]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["decision"]["behavior"] == "allow"

    other = dict(EVENT, tool_name="mcp__codex_app__fork_thread")
    monkeypatch.setattr(approval.sys, "stdin", Stdin(json.dumps(other).encode()))
    assert approval.main(["--store", str(path), "--grant-id", GRANT_ID]) == 0
    assert capsys.readouterr().out.strip() == "{}"


@pytest.mark.parametrize("payload", [b"not json", b'{"tool_name":"x","tool_name":"y"}', b'{"x":NaN}'])
def test_parser_failures_report_error_without_allow(tmp_path, monkeypatch, capsys, payload):
    monkeypatch.setattr(approval.sys, "stdin", Stdin(payload))
    assert approval.main(["--store", str(tmp_path / "absent.sqlite"), "--grant-id", GRANT_ID]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "decision=no-decision" in captured.err


def test_oversized_input_has_no_allow(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(approval.sys, "stdin", Stdin(b" " * (approval.MAX_INPUT_BYTES + 1)))
    assert approval.main(["--store", str(tmp_path / "absent.sqlite"), "--grant-id", GRANT_ID]) == 1
    assert not capsys.readouterr().out


def test_database_failure_has_no_allow(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(approval.sys, "stdin", Stdin(json.dumps(EVENT).encode()))
    assert approval.main(["--store", str(tmp_path / "absent.sqlite"), "--grant-id", GRANT_ID]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "decision=no-decision" in captured.err
