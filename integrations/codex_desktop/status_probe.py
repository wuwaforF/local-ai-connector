"""Read thread metadata from a matching Codex Desktop IPC snapshot.

This probe subscribes to the existing thread stream, reads one owner snapshot,
then unsubscribes. It never starts or changes a turn.
"""

from __future__ import annotations

import argparse
import errno
import json
from pathlib import Path
import socket
import sys
import time
from uuid import UUID, uuid4

import probe as base


SNAPSHOT_METHOD = "thread-stream-state-changed"
FOLLOWING_METHOD = "thread-stream-following-changed"
FOLLOWING_VERSION = 1
SNAPSHOT_VERSION = 11
RUNTIME_STATUSES = {"active", "idle", "systemError"}
TURN_STATUSES = {"inProgress", "completed", "failed", "interrupted"}


def _write(connection, message: dict, deadline: float) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if not payload or len(payload) > base.MAX_FRAME_BYTES:
        raise base.ProbeError("Desktop IPC message exceeds the frame limit")
    connection.settimeout(base._remaining(deadline))
    try:
        connection.sendall(len(payload).to_bytes(4, "little") + payload)
    except (TimeoutError, socket.timeout):
        raise base.ProbeError("Desktop IPC status probe timed out") from None
    except OSError as exc:
        raise base.ProbeError(f"Desktop IPC write failed: {exc.strerror or type(exc).__name__}") from None


def _read_message(connection, deadline: float) -> dict:
    length = int.from_bytes(base._read_exact(connection, 4, deadline), "little")
    if length <= 0 or length > base.MAX_FRAME_BYTES:
        raise base.ProbeError("Desktop IPC returned an invalid or oversized frame")
    try:
        message = json.loads(base._read_exact(connection, length, deadline))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise base.ProbeError("Desktop IPC returned malformed JSON") from None
    if not isinstance(message, dict):
        raise base.ProbeError("Desktop IPC returned a non-object message")
    return message


def _reply_to_inbound(connection, message: dict, deadline: float) -> None:
    request_id = message.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        raise base.ProbeError("Desktop IPC inbound request had no valid request ID")
    if message["type"] == "client-discovery-request":
        reply = {"type": "client-discovery-response", "requestId": request_id,
                 "response": {"canHandle": False}}
    else:
        reply = {"type": "response", "requestId": request_id,
                 "resultType": "error", "error": "no-handler-for-request"}
    _write(connection, reply, deadline)


def _broadcast(connection, client_id: str, owner_id: str, thread_id: str,
               following: bool, deadline: float) -> None:
    _write(connection, {
        "type": "broadcast",
        "method": FOLLOWING_METHOD,
        "sourceClientId": client_id,
        "targetClientIds": [owner_id],
        "version": FOLLOWING_VERSION,
        "params": {"conversationId": thread_id, "hostId": "local", "following": following},
    }, deadline)


def _metadata(message: dict, thread_id: str) -> dict:
    change = message.get("params", {}).get("change")
    conversation = change.get("conversationState") if isinstance(change, dict) else None
    if not isinstance(conversation, dict):
        conversation = {}

    result = {
        "thread_id": thread_id,
        "owner_found": True,
        "snapshot_revision": change.get("revision") if isinstance(change, dict) else None,
        "conversation_id": message.get("params", {}).get("conversationId"),
        "latest_turn_id": None,
        "latest_turn_status": "unknown",
        "latest_turn_sleeping": None,
        "runtime_status": "unknown",
    }
    runtime_status = conversation.get("threadRuntimeStatus")
    if isinstance(runtime_status, dict) and runtime_status.get("type") in RUNTIME_STATUSES:
        result["runtime_status"] = runtime_status["type"]
    cwd = conversation.get("cwd")
    if isinstance(cwd, str) and cwd and len(cwd) <= 1000:
        result["cwd"] = cwd

    turns = conversation.get("turns")
    latest = turns[-1] if isinstance(turns, list) and turns and isinstance(turns[-1], dict) else None
    if latest is not None:
        turn_id = latest.get("turnId", latest.get("id"))
        status = latest.get("status")
        sleeping = latest.get("isSleeping")
        if isinstance(turn_id, str) and turn_id:
            result["latest_turn_id"] = turn_id
        if isinstance(status, str) and status in TURN_STATUSES:
            result["latest_turn_status"] = status
        if isinstance(sleeping, bool):
            result["latest_turn_sleeping"] = sleeping
    else:
        status = conversation.get("lastTurnStatus", conversation.get("latestTurnStatus"))
        turn_id = conversation.get("lastTurnId", conversation.get("latestTurnId"))
        sleeping = conversation.get("lastTurnIsSleeping")
        if isinstance(turn_id, str) and turn_id:
            result["latest_turn_id"] = turn_id
        if isinstance(status, str) and status in TURN_STATUSES:
            result["latest_turn_status"] = status
        if isinstance(sleeping, bool):
            result["latest_turn_sleeping"] = sleeping

    revision = result["snapshot_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        result["snapshot_revision"] = None
    conversation_id = result["conversation_id"]
    if not isinstance(conversation_id, str) or len(conversation_id) > 200:
        result["conversation_id"] = None
    return result


def with_snapshot(thread_id: str, socket_path: Path, consume) -> dict:
    try:
        parsed_id = UUID(thread_id)
    except (ValueError, AttributeError):
        raise base.ProbeError("Thread ID must be a UUID") from None
    if str(parsed_id) != thread_id:
        raise base.ProbeError("Thread ID must use canonical UUID form")

    identity = base.validate_target(socket_path)
    deadline = time.monotonic() + base.TOTAL_TIMEOUT_SECONDS
    connection = None
    client_id = owner_id = None
    subscribed = False
    try:
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(base._remaining(deadline))
            connection.connect(str(socket_path))
        except (TimeoutError, socket.timeout):
            raise base.ProbeError("Desktop IPC connection timed out") from None
        except OSError as exc:
            raise base.ProbeError(f"Desktop IPC connection failed: {exc.strerror or type(exc).__name__}",
                                 kind="startup" if exc.errno in (errno.ENOENT, errno.ECONNREFUSED) else "permissions" if exc.errno in (errno.EPERM, errno.EACCES) else "unavailable") from None
        if base.validate_target(socket_path) != identity:
            raise base.ProbeError("Desktop IPC socket changed during connection", kind="invalid_socket")

        initialized = base._send_request(
            connection, request_id=str(uuid4()), source_client_id=base.INITIAL_CLIENT_ID,
            method="initialize", version=0,
            params={"clientType": "local-ai-connector-status-probe"}, deadline=deadline,
        ).get("result")
        if not isinstance(initialized, dict) or not isinstance(initialized.get("clientId"), str) or not initialized["clientId"]:
            raise base.ProbeError("Desktop IPC initialize returned no client ID")
        client_id = initialized["clientId"]

        discovery = base._send_request(
            connection, request_id=str(uuid4()), source_client_id=client_id,
            method="thread-owner-discovery", version=1,
            params={"hostId": "local", "conversationId": thread_id}, deadline=deadline,
        )
        owner_id = discovery.get("handledByClientId")
        if not isinstance(owner_id, str) or not owner_id:
            raise base.ProbeError("Desktop IPC found no owner for the requested thread", kind="no_owner")

        _broadcast(connection, client_id, owner_id, thread_id, True, deadline)
        subscribed = True
        while True:
            message = _read_message(connection, deadline)
            message_type = message.get("type")
            if message_type in ("request", "client-discovery-request"):
                _reply_to_inbound(connection, message, deadline)
                continue
            if message_type != "broadcast" or message.get("method") != SNAPSHOT_METHOD:
                continue
            params = message.get("params")
            if not isinstance(params, dict) or params.get("conversationId") != thread_id:
                continue
            if message.get("version") != SNAPSHOT_VERSION:
                raise base.ProbeError("Desktop IPC snapshot version did not match", kind="protocol")
            if params.get("hostId") != "local":
                raise base.ProbeError("Desktop IPC snapshot host did not match", kind="stale_target")
            if message.get("sourceClientId") != owner_id:
                raise base.ProbeError("Desktop IPC snapshot came from an unexpected owner", kind="stale_target")
            change = params.get("change")
            if not isinstance(change, dict) or change.get("type") != "snapshot":
                continue
            conversation = change.get("conversationState")
            if not isinstance(conversation, dict) or conversation.get("id") != thread_id:
                raise base.ProbeError("Desktop IPC snapshot thread identity did not match", kind="stale_target")
            return consume(connection, client_id, owner_id, message)
    finally:
        if connection is not None:
            if subscribed and client_id and owner_id:
                try:
                    _broadcast(connection, client_id, owner_id, thread_id, False,
                               max(deadline, time.monotonic() + 0.1))
                except base.ProbeError:
                    pass
            connection.close()


def probe(thread_id: str, socket_path: Path) -> dict:
    return with_snapshot(thread_id, socket_path,
                         lambda _connection, _client_id, _owner_id, message: _metadata(message, thread_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Codex Desktop thread snapshot probe")
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--socket-path", type=Path, default=Path.home() / ".codex" / "ipc" / "ipc.sock")
    args = parser.parse_args(argv)
    try:
        result = probe(args.thread_id, args.socket_path)
    except base.ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
