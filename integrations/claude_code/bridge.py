"""CommandAdapter bridge to an explicitly captured Claude Code session inbox."""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import sys
from uuid import UUID


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from local_ai_connector.claude_binding import TARGET_FIELDS, StaleTarget, socket_identity, confirmed_session

SOCKET_TIMEOUT = 5


def handle(request, session_record):
    write_started = False
    try:
        op = request["op"]
        if op not in ("confirm", "status", "reconcile", "send"):
            raise ValueError("Unsupported bridge operation")
        snapshot = confirmed_session(request["target"], session_record)
        target = {field: snapshot[field] for field in TARGET_FIELDS}
        if op == "confirm":
            return {"ok": True, "target": target}
        if op == "status":
            return {"ok": True, "state": "unknown"}
        if op == "reconcile":
            return {"ok": True, "result": "unknown"}
        dispatch = str(UUID(request["dispatch_id"]))
        text = request["text"]
        if (dispatch != request["dispatch_id"] or not isinstance(text, str)
                or not 0 < len(text) <= 1000 or dispatch not in text):
            raise ValueError("Expected a bounded wake instruction containing its dispatch UUID")
        frame = {"msgV": 1, "msg_id": dispatch, "type": "user",
                 "message": {"role": "user", "content": text},
                 "priority": "next", "session_id": target["session_id"]}
        payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(SOCKET_TIMEOUT)
            connection.connect(snapshot["socket_path"])
            if confirmed_session(target, session_record) != snapshot:
                raise StaleTarget("The session inbox changed while connecting")
            # A partial write can already cause a turn. No exception after this point proves non-delivery.
            write_started = True
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
        # The inbox protocol provides no application acknowledgment for this one-way frame.
        return {"ok": False, "error": "ambiguous", "detail": "Inbox frame written; delivery is unconfirmed"}
    except Exception as exc:
        if write_started:
            return {"ok": False, "error": "ambiguous", "detail": type(exc).__name__}
        if isinstance(exc, StaleTarget):
            return {"ok": False, "error": "stale_target", "detail": str(exc)}
        if isinstance(exc, OSError):
            return {"ok": False, "error": "unavailable", "detail": type(exc).__name__}
        if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
            return {"ok": False, "error": "rejected", "detail": "Invalid bridge request"}
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-record", required=True, type=Path)
    args = parser.parse_args()
    try:
        response = handle(json.load(sys.stdin), args.session_record)
    except (ValueError, UnicodeError):
        response = {"ok": False, "error": "rejected", "detail": "Invalid JSON request"}
    print(json.dumps(response))
