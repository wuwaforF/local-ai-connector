"""CommandAdapter bridge to a Claude Code session inbox.

The target is either an explicitly captured session (a session record names its inbox), or the
chat a task was pinned to at approval, {"session": "claude:<session id>"}. A pinned session is
resolved through Claude Code's own registry of running sessions, which also says whether the
session is idle or busy.
"""
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
DEFAULT_SESSIONS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "sessions"


class Unavailable(Exception):
    pass


def pinned_session(target, sessions_dir):
    """The one running Claude Code process that holds a pinned session, read from Claude Code's registry."""
    if not isinstance(target, dict) or set(target) != {"session"} or not isinstance(target["session"], str):
        raise ValueError("A pinned target names one session")
    namespace, _, session_id = target["session"].partition(":")
    if namespace != "claude" or str(UUID(session_id)) != session_id:
        raise ValueError("The pinned session is not a Claude Code session")
    live = []
    for path in sorted(Path(sessions_dir).glob("*.json")):
        if not path.stem.isdigit():
            continue
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    continue
                record = json.load(file)
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("sessionId") != session_id:
            continue
        pid, socket_path = record.get("pid"), record.get("messagingSocketPath")
        if (type(pid) is not int or str(pid) != path.stem or not isinstance(socket_path, str)
                or not Path(socket_path).is_absolute() or "\0" in socket_path):
            continue
        try:
            os.kill(pid, 0)  # the registry can outlive its process
            inbox = socket_identity(socket_path)
        except (OSError, StaleTarget):
            continue
        live.append({"pid": pid, "socket_path": socket_path, "socket_dev": inbox.st_dev,
                     "socket_ino": inbox.st_ino, "status": record.get("status")})
    if len(live) != 1:
        raise Unavailable("The pinned Claude Code session is not running" if not live else
                          "More than one running process claims the pinned session")
    return live[0]


def _inbox(snapshot):
    return {key: snapshot[key] for key in ("socket_path", "socket_dev", "socket_ino")}


def handle(request, session_record=None, sessions_dir=DEFAULT_SESSIONS):
    write_started = False
    try:
        op = request["op"]
        if op not in ("confirm", "status", "reconcile", "send"):
            raise ValueError("Unsupported bridge operation")
        pinned = isinstance(request["target"], dict) and set(request["target"]) == {"session"}
        if pinned:
            snapshot = pinned_session(request["target"], sessions_dir)
            target = {"session": request["target"]["session"]}
            session_id = target["session"].partition(":")[2]
        else:
            if session_record is None:
                raise ValueError("A captured target needs its session record")
            snapshot = confirmed_session(request["target"], session_record)
            target = {field: snapshot[field] for field in TARGET_FIELDS}
            session_id = target["session_id"]

        def current():
            return pinned_session(target, sessions_dir) if pinned else confirmed_session(target, session_record)

        if op == "confirm":
            return {"ok": True, "target": target}
        if op == "status":
            state = snapshot.get("status") if pinned else None
            return {"ok": True, "state": state if state in ("idle", "busy") else "unknown"}
        if op == "reconcile":
            return {"ok": True, "result": "unknown"}
        dispatch = str(UUID(request["dispatch_id"]))
        text = request["text"]
        if (dispatch != request["dispatch_id"] or not isinstance(text, str)
                or not 0 < len(text) <= 1000 or dispatch not in text):
            raise ValueError("Expected a bounded wake instruction containing its dispatch UUID")
        frame = {"msgV": 1, "msg_id": dispatch, "type": "user",
                 "message": {"role": "user", "content": text},
                 "priority": "next", "session_id": session_id}
        payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(SOCKET_TIMEOUT)
            connection.connect(snapshot["socket_path"])
            if _inbox(current()) != _inbox(snapshot):
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
        if isinstance(exc, Unavailable):
            return {"ok": False, "error": "unavailable", "detail": str(exc)}
        if isinstance(exc, StaleTarget):
            return {"ok": False, "error": "stale_target", "detail": str(exc)}
        if isinstance(exc, OSError):
            return {"ok": False, "error": "unavailable", "detail": type(exc).__name__}
        if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
            return {"ok": False, "error": "rejected", "detail": "Invalid bridge request"}
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-record", type=Path, help="captured session record (fixed targets only)")
    parser.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS,
                        help="Claude Code's registry of running sessions (pinned targets)")
    args = parser.parse_args()
    try:
        response = handle(json.load(sys.stdin), args.session_record, args.sessions_dir)
    except (ValueError, UnicodeError):
        response = {"ok": False, "error": "rejected", "detail": "Invalid JSON request"}
    print(json.dumps(response))
