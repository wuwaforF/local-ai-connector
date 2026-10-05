"""Isolated one-use PermissionRequest hook prototype for create_thread.

The grant store must be provisioned by a trusted administrator or broker. This
module only consumes rows; it does not mint or persist grants from hook input.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

EVENT = "PermissionRequest"
TOOL = "mcp__codex_app__create_thread"
MAX_INPUT_BYTES = 1024 * 1024

# Provision out of band. There is deliberately no production schema initializer.
GRANT_SCHEMA_SQL = """CREATE TABLE creation_grants (
    grant_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_input_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
)"""


class HookError(Exception):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _json_value(raw: str):
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON number")))


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _database_uri(path: Path) -> str:
    absolute = path.expanduser().resolve()
    return absolute.as_uri() + "?mode=rw"


def consume_grant(event: dict, store: Path, grant_id: str, now: float | None = None) -> tuple[bool, str]:
    required = ("session_id", "cwd", "tool_name", "tool_input")
    if any(key not in event for key in required):
        return False, "event-fields-missing"
    if (not isinstance(event["session_id"], str) or not isinstance(event["cwd"], str)
            or not isinstance(event["tool_name"], str) or not isinstance(event["tool_input"], dict)):
        return False, "event-fields-invalid"

    connection = sqlite3.connect(_database_uri(store), uri=True, timeout=5, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT session_id,cwd,tool_name,tool_input_json,expires_at,consumed_at "
            "FROM creation_grants WHERE grant_id=?", (grant_id,),
        ).fetchone()
        if row is None:
            reason = "grant-missing"
        else:
            session_id, cwd, tool_name, stored_input, expires_at, consumed_at = row
            if consumed_at is not None:
                reason = "grant-used"
            elif not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at) or (time.time() if now is None else now) >= expires_at:
                reason = "grant-expired"
            else:
                try:
                    same_input = _canonical(_json_value(stored_input)) == _canonical(event["tool_input"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    same_input = False
                if (session_id, cwd, tool_name, same_input) != (
                    event["session_id"], event["cwd"], TOOL, True,
                ) or event["tool_name"] != TOOL:
                    reason = "grant-mismatch"
                else:
                    consumed_at = time.time() if now is None else now
                    if consumed_at >= expires_at:
                        connection.execute("ROLLBACK")
                        return False, "grant-expired"
                    updated = connection.execute(
                        "UPDATE creation_grants SET consumed_at=? "
                        "WHERE grant_id=? AND consumed_at IS NULL AND expires_at>?",
                        (consumed_at, grant_id, consumed_at),
                    )
                    if updated.rowcount != 1:
                        reason = "grant-used"
                    else:
                        connection.execute("COMMIT")
                        return True, "grant-consumed"
        connection.execute("ROLLBACK")
        return False, reason
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def evaluate(event, store: Path, grant_id: str, now: float | None = None):
    if not isinstance(event, dict):
        return False, "event-invalid"
    if event.get("hook_event_name") != EVENT or event.get("tool_name") != TOOL:
        return None, "unmatched"
    return consume_grant(event, store, grant_id, now)


def _response(decision: bool) -> dict:
    behavior = "allow" if decision else "deny"
    result = {"behavior": behavior}
    if not decision:
        result["message"] = "Creation grant is invalid, expired, or already used."
    return {"hookSpecificOutput": {"hookEventName": EVENT, "decision": result}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--grant-id", required=True)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise HookError("hook input exceeds size limit")
        event = _json_value(raw.decode("utf-8"))
        decision, reason = evaluate(event, args.store, args.grant_id)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError, HookError, OSError, sqlite3.Error) as exc:
        print(f"grant_id={args.grant_id} decision=no-decision reason={type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({} if decision is None else _response(decision), separators=(",", ":")))
    outcome = "no-decision" if decision is None else "allow" if decision else "deny"
    print(f"grant_id={args.grant_id} decision={outcome} reason={reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
