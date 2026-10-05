"""CommandAdapter bridge for one explicitly pinned Codex Desktop thread."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

import probe
import status_probe
from local_ai_connector.wakeup import WAKE_TEXT


DEFAULT_SOCKET = Path.home() / ".codex" / "ipc" / "ipc.sock"
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "local-ai-connector" / "codex-desktop-dispatches"
MAX_WAKE_TEXT = 1000
TURN_START_VERSION = 2


class BridgeError(ValueError):
    def __init__(self, kind: str, detail: str, *, retryable: bool = False):
        super().__init__(detail)
        self.kind, self.retryable = kind, retryable


def _target(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"thread_id", "workspace"}:
        raise BridgeError("rejected", "Explicit thread_id and workspace are required")
    thread_id, workspace = value["thread_id"], value["workspace"]
    try:
        if not isinstance(thread_id, str) or str(UUID(thread_id)) != thread_id:
            raise ValueError
    except ValueError:
        raise BridgeError("rejected", "The pinned thread ID is invalid") from None
    if not isinstance(workspace, str) or not Path(workspace).is_absolute() or "\0" in workspace:
        raise BridgeError("rejected", "The pinned workspace must be an absolute path")
    return {"thread_id": thread_id, "workspace": workspace}


def _state_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise BridgeError("rejected", "The dispatch state directory must be absolute")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(path)
    except OSError as exc:
        raise BridgeError("unavailable", f"Dispatch state unavailable: {type(exc).__name__}") from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise BridgeError("rejected", "The dispatch state directory must be private and user-owned")
    return path


def _record_path(state_dir: Path, dispatch_id: str) -> Path:
    try:
        if str(UUID(dispatch_id)) != dispatch_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise BridgeError("rejected", "The dispatch ID is invalid") from None
    return state_dir / f"{dispatch_id}.json"


def _is_wake_text(text: object, dispatch_id: str) -> bool:
    if not isinstance(text, str) or not 0 < len(text) <= MAX_WAKE_TEXT or "\0" in text:
        return False
    template = WAKE_TEXT.format(dispatch=dispatch_id, after="__AFTER__")
    pattern = re.escape(template).replace(re.escape("__AFTER__"), r"[0-9]+")
    return re.fullmatch(pattern, text) is not None


def _read_record(path: Path, dispatch_id: str, target: dict) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BridgeError("ambiguous", f"Dispatch record unavailable: {type(exc).__name__}") from None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise BridgeError("ambiguous", "Dispatch record is not a private regular file")
            record = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise BridgeError("ambiguous", "Dispatch record cannot be verified") from None
    if (not isinstance(record, dict) or record.get("dispatch_id") != dispatch_id
            or record.get("target") != target or record.get("state") not in ("intent", "acknowledged")):
        raise BridgeError("ambiguous", "Dispatch record identity is inconsistent")
    if record["state"] == "acknowledged" and (not isinstance(record.get("turn_id"), str) or not record["turn_id"]):
        raise BridgeError("ambiguous", "Acknowledgment record has no turn ID")
    return record


def _write_new_intent(path: Path, dispatch_id: str, target: dict) -> dict | None:
    record = {"dispatch_id": dispatch_id, "target": target, "state": "intent"}
    payload = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return _read_record(path, dispatch_id, target)
    except OSError as exc:
        raise BridgeError("unavailable", f"Could not create dispatch intent: {type(exc).__name__}") from None
    try:
        with os.fdopen(fd, "wb", closefd=False) as file:
            file.write(payload)
            file.flush()
            os.fsync(fd)
    except OSError as exc:
        raise BridgeError("ambiguous", f"Dispatch intent write failed: {type(exc).__name__}") from None
    finally:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return None


def _write_ack(path: Path, dispatch_id: str, target: dict, turn_id: str) -> None:
    record = {"dispatch_id": dispatch_id, "target": target,
              "state": "acknowledged", "turn_id": turn_id}
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="dispatch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write((json.dumps(record, separators=(",", ":")) + "\n").encode())
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        Path(temporary).unlink(missing_ok=True)
        raise BridgeError("ambiguous", f"Dispatch acknowledgment write failed: {type(exc).__name__}") from None


def _snapshot_matches(target: dict, message: dict) -> dict:
    metadata = status_probe._metadata(message, target["thread_id"])
    if metadata.get("conversation_id") != target["thread_id"] or metadata.get("cwd") != target["workspace"]:
        raise BridgeError("stale_target", "Codex Desktop thread or workspace does not match the pinned target")
    return metadata


def _host_state(metadata: dict, conversation: dict) -> str:
    runtime = metadata.get("runtime_status")
    if runtime == "active":
        return "busy"
    if runtime != "idle":
        return "unknown"
    requests = conversation.get("requests")
    if not isinstance(requests, list) or requests:
        return "unknown"
    return "idle"


def _turn_ack(response: dict, owner_id: str) -> str:
    if response.get("handledByClientId") != owner_id:
        raise BridgeError("ambiguous", "Desktop start-turn acknowledgment came from an unexpected owner")
    result = response.get("result")
    nested = result.get("result") if isinstance(result, dict) else None
    turn = nested.get("turn") if isinstance(nested, dict) else None
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise BridgeError("ambiguous", "Desktop start-turn acknowledgment had no turn ID")
    return turn_id


def _start_turn(connection, client_id: str, owner_id: str, target: dict,
                dispatch_id: str, text: str) -> str:
    response = probe._send_request(
        connection, request_id=str(uuid4()), source_client_id=client_id,
        target_client_id=owner_id, method="thread-follower-start-turn",
        version=TURN_START_VERSION,
        params={"conversationId": target["thread_id"], "turnStart": {
            "request": {"threadId": target["thread_id"], "input": [
                {"type": "text", "text": text, "text_elements": []},
            ]},
            "context": {"inheritThreadSettings": True},
        }},
        deadline=time.monotonic() + probe.TOTAL_TIMEOUT_SECONDS,
    )
    return _turn_ack(response, owner_id)


def handle(request: dict, *, socket_path: Path = DEFAULT_SOCKET,
           state_dir: Path = DEFAULT_STATE_DIR) -> dict:
    side_effect_started = False
    try:
        if not isinstance(request, dict):
            raise BridgeError("rejected", "Bridge request must be an object")
        op = request.get("op")
        if op not in ("confirm", "status", "restore", "send", "reconcile"):
            raise BridgeError("rejected", "Unsupported bridge operation")
        expected_fields = {
            "confirm": {"op", "target"},
            "status": {"op", "target"},
            "restore": {"op", "target"},
            "send": {"op", "target", "dispatch_id", "text"},
            "reconcile": {"op", "target", "dispatch_id"},
        }[op]
        if set(request) != expected_fields:
            raise BridgeError("rejected", "Unexpected bridge request fields")
        target = _target(request.get("target"))

        if op == "restore":
            # A target can become loaded since the dispatcher's last check. Opening it
            # then would steal focus unnecessarily, so inspect again before navigating.
            try:
                def already_loaded(_connection, _client_id, _owner_id, message):
                    _snapshot_matches(target, message)
                    return {"ok": True, "restored": False}
                return status_probe.with_snapshot(target["thread_id"], socket_path, already_loaded)
            except probe.ProbeError as exc:
                if exc.kind not in ("no_owner", "socket_missing", "startup", "owner_discovery_timeout"):
                    raise
            result = subprocess.run(
                ["/usr/bin/open", "-b", "com.openai.codex", "codex://threads/" + target["thread_id"]],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            if result.returncode != 0:
                raise BridgeError("unavailable", "Codex Desktop navigation was not accepted")
            return {"ok": True, "restored": True}

        if op == "send":
            dispatch_id, text = request.get("dispatch_id"), request.get("text")
            record_dir = _state_directory(state_dir)
            path = _record_path(record_dir, dispatch_id)
            old = _read_record(path, dispatch_id, target)
            if old is not None:
                if old["state"] == "acknowledged":
                    return {"ok": True, "accepted": True, "host_ref": old["turn_id"]}
                return {"ok": False, "error": "ambiguous", "detail": "Dispatch was already attempted"}
            if not _is_wake_text(text, dispatch_id):
                raise BridgeError("rejected", "Expected the connector's bounded, body-free wake instruction")

            def send_if_idle(connection, client_id, owner_id, message):
                metadata = _snapshot_matches(target, message)
                conversation = message["params"]["change"]["conversationState"]
                if _host_state(metadata, conversation) != "idle":
                    raise BridgeError("rejected", "Codex Desktop thread is not confirmed idle", retryable=True)
                existing = _write_new_intent(path, dispatch_id, target)
                if existing is not None:
                    if existing["state"] == "acknowledged":
                        return {"ok": True, "accepted": True, "host_ref": existing["turn_id"]}
                    return {"ok": False, "error": "ambiguous", "detail": "Dispatch was already attempted"}
                nonlocal side_effect_started
                side_effect_started = True
                turn_id = _start_turn(connection, client_id, owner_id, target, dispatch_id, text)
                _write_ack(path, dispatch_id, target, turn_id)
                return {"ok": True, "accepted": True, "host_ref": turn_id}

            return status_probe.with_snapshot(target["thread_id"], socket_path, send_if_idle)

        def inspect(_connection, _client_id, _owner_id, message):
            metadata = _snapshot_matches(target, message)
            conversation = message["params"]["change"]["conversationState"]
            if op == "confirm":
                return {"ok": True, "target": target}
            return {"ok": True, "state": _host_state(metadata, conversation)}

        if op in ("confirm", "status"):
            return status_probe.with_snapshot(target["thread_id"], socket_path, inspect)

        record_dir = _state_directory(state_dir)
        record = _read_record(_record_path(record_dir, request.get("dispatch_id")),
                              request.get("dispatch_id"), target)
        if record is None:
            return {"ok": True, "result": "unknown"}
        return {"ok": True, "result": "delivered" if record["state"] == "acknowledged" else "unknown"}
    except BridgeError as exc:
        kind = "ambiguous" if side_effect_started and exc.kind != "ambiguous" else exc.kind
        return {"ok": False, "error": kind, "detail": str(exc)[:200],
                **({"retryable": True} if exc.retryable and not side_effect_started else {})}
    except probe.ProbeError as exc:
        kind = "ambiguous" if side_effect_started else exc.kind
        return {"ok": False, "error": kind, "detail": str(exc)[:200],
                **({"retryable": True} if op == "send" and not side_effect_started and kind in ("no_owner", "socket_missing", "startup", "owner_discovery_timeout") else {})}
    except (OSError, TimeoutError, socket.timeout, subprocess.TimeoutExpired) as exc:
        kind = "ambiguous" if side_effect_started else "unavailable"
        return {"ok": False, "error": kind, "detail": type(exc).__name__}
    except (ValueError, KeyError, TypeError) as exc:
        kind = "ambiguous" if side_effect_started else "rejected"
        return {"ok": False, "error": kind, "detail": "Invalid bridge request"}


def main(argv: list[str] | None = None) -> int:
    parser_ = argparse.ArgumentParser(description=__doc__)
    parser_.add_argument("--socket-path", type=Path, default=DEFAULT_SOCKET)
    parser_.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser_.parse_args(argv)
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError
        reply = handle(request, socket_path=args.socket_path, state_dir=args.state_dir)
    except (ValueError, UnicodeError, json.JSONDecodeError):
        reply = {"ok": False, "error": "rejected", "detail": "Invalid bridge request"}
    print(json.dumps(reply, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
