"""CommandAdapter bridge to a Claude Code session inbox.

The target is either an explicitly captured session (a session record names its inbox), or the
chat a task was pinned to at approval, {"session": "claude:<session id>"}. A pinned session is
resolved through Claude Code's own registry of running sessions, which also says whether the
session is idle or busy.
"""
import argparse
import calendar
import json
import os
from pathlib import Path
import socket
import sys
import time
from uuid import UUID


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from local_ai_connector import os_adapter
from local_ai_connector.claude_binding import TARGET_FIELDS, StaleTarget, confirmed_session

SOCKET_TIMEOUT = 5
DEFAULT_SESSIONS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "sessions"


class Unavailable(Exception):
    pass


class Refused(Exception):
    """A request this platform or target cannot serve at all."""


# Windows FILETIME counts 100 ns intervals from 1601-01-01; this many separate it from the Unix epoch.
_FILETIME_UNIX_EPOCH = 116444736000000000


def _started_at(text):
    """Claude Code's procStart in Unix seconds, None if unrecognized.

    macOS records it like "Sat Oct 24 15:30:23 2026" (UTC); Windows records a FILETIME as decimal digits.
    """
    if isinstance(text, str) and text.isdecimal():
        return (int(text) - _FILETIME_UNIX_EPOCH) / 10_000_000
    try:
        return float(calendar.timegm(time.strptime(text, "%a %b %d %H:%M:%S %Y")))
    except (TypeError, ValueError):
        return None


def pinned_session(target, sessions_dir):
    """The one running Claude Code process that holds a pinned session, read from Claude Code's registry."""
    if not isinstance(target, dict) or set(target) != {"session"} or not isinstance(target["session"], str):
        raise ValueError("A pinned target names one session")
    namespace, _, session_id = target["session"].partition(":")
    if namespace != "claude" or str(UUID(session_id)) != session_id:
        raise ValueError("The pinned session is not a Claude Code session")
    if not os_adapter.supports("local_ipc"):
        raise Unavailable("Claude Code's session inbox is not supported on this platform yet")
    live, unreadable = [], 0
    for path in sorted(Path(sessions_dir).glob("*.json")):
        if not path.stem.isdigit():
            continue
        try:
            record = json.loads(os_adapter.read_owned_text(path))  # UTF-8 whatever the locale
        except (OSError, UnicodeError, ValueError):
            unreadable += 1
            continue
        if not isinstance(record, dict) or record.get("sessionId") != session_id:
            continue
        pid, socket_path = record.get("pid"), record.get("messagingSocketPath")
        if (type(pid) is not int or str(pid) != path.stem or not isinstance(socket_path, str)
                or not Path(socket_path).is_absolute() or "\0" in socket_path):
            continue
        if not os_adapter.own_process_alive(pid):  # the registry can outlive its process
            continue
        recorded, actual = _started_at(record.get("procStart")), os_adapter.process_start_time(pid)
        if recorded is not None and actual is not None and abs(recorded - actual) > 5:
            continue  # the PID now belongs to another process
        if os_adapter.NAME == "windows":
            identity = None  # a named pipe is identified by its server process once connected (see send)
        else:
            try:
                identity = os_adapter.endpoint_identity(socket_path)
            except (OSError, os_adapter.NotPrivate):
                continue
        live.append({"pid": pid, "socket_path": socket_path, "identity": identity, "status": record.get("status"),
                     "recorded_start": recorded})
    if len(live) != 1:
        detail = ("The pinned Claude Code session is not running" if not live else
                  "More than one running process claims the pinned session")
        if unreadable and not live:
            detail += f" ({unreadable} session record(s) could not be read)"
        raise Unavailable(detail)
    return live[0]


def _inbox(snapshot):
    if "identity" in snapshot:
        return (snapshot["socket_path"], snapshot["pid"], snapshot["identity"])
    return (snapshot["socket_path"], snapshot["socket_dev"], snapshot["socket_ino"])


def _auth_line(credential, snapshot):
    """The first line a Windows inbox requires: the bound chat's own token, as Claude Code documents it."""
    token = credential.get("token") if isinstance(credential, dict) else None
    if not isinstance(token, str) or not token:
        raise Unavailable("Claude Code's Windows inbox needs the bound chat's own delivery token; the chat hands it "
                          "over when it next calls a connector tool")
    reported = credential.get("inbox")
    if reported is not None and os.path.normcase(str(reported)) != os.path.normcase(snapshot["socket_path"]):
        raise StaleTarget("The inbox the bound chat reported differs from Claude Code's session registry")
    return (json.dumps({"type": "auth", "token": token}) + "\n").encode("utf-8")


def _require_pinned_server(snapshot, connection):
    """A named pipe is trusted only if the registered process of the pinned session serves it."""
    try:
        _, pid, started = os_adapter.endpoint_identity(snapshot["socket_path"], connection)
    except os_adapter.NotPrivate as exc:
        raise Unavailable(f"The inbox could not be verified: {exc}") from None
    recorded = snapshot.get("recorded_start")
    if pid != snapshot["pid"] or (recorded is not None and started is not None and abs(started - recorded) > 5):
        raise StaleTarget("The inbox is not served by the pinned session's process")


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
            if os_adapter.NAME == "windows":
                raise Refused("Captured-session targets work on macOS and Linux only; bind the chat instead")
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
        windows = os_adapter.NAME == "windows"
        # Windows inboxes accept a connection only after the bound chat's own token (macOS and Linux need
        # none, so none is sent there). The token comes in the stdin request and is never echoed.
        auth = _auth_line(request.get("credential"), snapshot) if windows else b""
        with os_adapter.connect_local_ipc(snapshot["socket_path"], SOCKET_TIMEOUT) as connection:
            if windows:
                _require_pinned_server(snapshot, connection)
            if _inbox(current()) != _inbox(snapshot):
                raise StaleTarget("The session inbox changed while connecting")
            # A partial write can already cause a turn. No exception after this point proves non-delivery.
            write_started = True
            connection.sendall(auth + payload)
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
        if isinstance(exc, Refused):
            return {"ok": False, "error": "rejected", "detail": str(exc)}
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
