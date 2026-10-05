"""Read-only probe for the owner of one Codex Desktop thread.

The probe speaks only initialize and thread-owner-discovery over Desktop IPC.
It never starts an app-server, reads history, or submits or changes a turn.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time
from uuid import UUID, uuid4


MAX_FRAME_BYTES = 1024 * 1024
TOTAL_TIMEOUT_SECONDS = 5.0
INITIAL_CLIENT_ID = "initializing-client"


class ProbeError(Exception):
    """Expected, user-facing probe failure with a bounded recovery reason."""

    def __init__(self, detail: str, *, kind: str = "unavailable"):
        super().__init__(detail)
        self.kind = kind


class ProbeTimeout(ProbeError):
    """A local deadline expired; callers may distinguish the request phase."""


def validate_target(socket_path: Path) -> tuple[int, int]:
    if not socket_path.is_absolute() or "\0" in str(socket_path):
        raise ProbeError("Socket path must be absolute and contain no NUL bytes")

    try:
        parent = os.lstat(socket_path.parent)
    except OSError as exc:
        raise ProbeError(f"Desktop IPC socket unavailable: {exc.strerror or type(exc).__name__}",
                         kind="socket_missing" if exc.errno == errno.ENOENT else "permissions" if exc.errno in (errno.EPERM, errno.EACCES) else "unavailable") from None
    uid = os.getuid()
    if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != uid or stat.S_IMODE(parent.st_mode) & 0o077):
        raise ProbeError("Desktop IPC parent must be a current-user-owned private directory", kind="invalid_socket")
    try:
        target = os.lstat(socket_path)
    except OSError as exc:
        raise ProbeError(f"Desktop IPC socket unavailable: {exc.strerror or type(exc).__name__}",
                         kind="socket_missing" if exc.errno == errno.ENOENT else "permissions" if exc.errno in (errno.EPERM, errno.EACCES) else "unavailable") from None
    if (stat.S_ISLNK(target.st_mode) or not stat.S_ISSOCK(target.st_mode)
            or target.st_uid != uid or stat.S_IMODE(target.st_mode) & 0o077):
        raise ProbeError("Desktop IPC path must be a current-user-owned private socket, not a symlink", kind="invalid_socket")
    return target.st_dev, target.st_ino


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeTimeout("Desktop IPC probe timed out")
    return remaining


def _read_exact(connection: socket.socket, size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        connection.settimeout(_remaining(deadline))
        try:
            chunk = connection.recv(size - len(chunks))
        except (TimeoutError, socket.timeout):
            raise ProbeTimeout("Desktop IPC probe timed out") from None
        except OSError as exc:
            raise ProbeError(f"Desktop IPC read failed: {exc.strerror or type(exc).__name__}") from None
        if not chunk:
            raise ProbeError("Desktop IPC closed before replying")
        chunks.extend(chunk)
    return bytes(chunks)


def _receive_response(connection: socket.socket, request_id: str, method: str, deadline: float) -> dict:
    while True:
        length = int.from_bytes(_read_exact(connection, 4, deadline), "little")
        if length <= 0 or length > MAX_FRAME_BYTES:
            raise ProbeError("Desktop IPC returned an invalid or oversized frame")
        payload = _read_exact(connection, length, deadline)
        try:
            message = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProbeError("Desktop IPC returned malformed JSON") from None
        if not isinstance(message, dict):
            raise ProbeError("Desktop IPC returned a non-object message")
        if message.get("type") == "broadcast":
            continue
        message_type = message.get("type")
        if message_type in ("client-discovery-request", "request"):
            inbound_id = message.get("requestId")
            if not isinstance(inbound_id, str) or not inbound_id:
                raise ProbeError("Desktop IPC inbound request had no valid request ID")
            if message_type == "client-discovery-request":
                reply = {
                    "type": "client-discovery-response",
                    "requestId": inbound_id,
                    "response": {"canHandle": False},
                }
            else:
                reply = {
                    "type": "response",
                    "requestId": inbound_id,
                    "resultType": "error",
                    "error": "no-handler-for-request",
                }
            encoded = json.dumps(reply, separators=(",", ":")).encode("utf-8")
            if not encoded or len(encoded) > MAX_FRAME_BYTES:
                raise ProbeError("Desktop IPC reply exceeds the frame limit")
            connection.settimeout(_remaining(deadline))
            try:
                connection.sendall(len(encoded).to_bytes(4, "little") + encoded)
            except (TimeoutError, socket.timeout):
                raise ProbeError("Desktop IPC probe timed out") from None
            except OSError as exc:
                raise ProbeError(f"Desktop IPC write failed: {exc.strerror or type(exc).__name__}") from None
            continue
        if message_type != "response":
            type_repr = repr(message_type)
            if len(type_repr) > 60:
                type_repr = type_repr[:57] + "..."
            raise ProbeError(f"Desktop IPC returned an unexpected message type: {type_repr}")
        if message.get("requestId") != request_id:
            raise ProbeError("Desktop IPC response request ID did not match")
        if "method" in message and message["method"] != method:
            raise ProbeError("Desktop IPC response method did not match")
        if message.get("resultType") != "success":
            error = str(message.get("error") or "request failed").split(":", 1)[0]
            raise ProbeError(f"Desktop IPC request failed: {error}",
                             kind="no_owner" if method == "thread-owner-discovery" and error in ("no-client-found", "no-handler-for-request") else "protocol")
        return message


def _send_request(connection: socket.socket, *, request_id: str, source_client_id: str,
                  method: str, version: int, params: dict, deadline: float,
                  target_client_id: str | None = None) -> dict:
    message = {
        "type": "request",
        "requestId": request_id,
        "sourceClientId": source_client_id,
        "version": version,
        "method": method,
        "params": params,
        "timeoutMs": max(1, int(_remaining(deadline) * 1000)),
    }
    if target_client_id:
        message["targetClientId"] = target_client_id
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise ProbeError("Desktop IPC request exceeds the frame limit")
    connection.settimeout(_remaining(deadline))
    try:
        connection.sendall(len(payload).to_bytes(4, "little") + payload)
    except (TimeoutError, socket.timeout):
        raise ProbeError("Desktop IPC probe timed out") from None
    except OSError as exc:
        raise ProbeError(f"Desktop IPC write failed: {exc.strerror or type(exc).__name__}") from None
    try:
        return _receive_response(connection, request_id, method, deadline)
    except ProbeTimeout:
        if method != "thread-owner-discovery":
            raise
        # Desktop's router can spend 10 seconds discovering clients, beyond this
        # probe's 5-second deadline. A timeout allows opted-in navigation only;
        # a fresh owner snapshot must still establish identity and readiness.
        raise ProbeError("Desktop IPC thread owner discovery timed out",
                         kind="owner_discovery_timeout") from None


def probe(thread_id: str, socket_path: Path) -> dict:
    try:
        parsed_id = UUID(thread_id)
    except (ValueError, AttributeError):
        raise ProbeError("Thread ID must be a UUID") from None
    if str(parsed_id) != thread_id:
        raise ProbeError("Thread ID must use canonical UUID form")

    identity = validate_target(socket_path)
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    connection = None
    try:
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            raise ProbeError(f"Desktop IPC socket creation failed: {exc.strerror or type(exc).__name__}") from None
        connection.settimeout(_remaining(deadline))
        try:
            connection.connect(str(socket_path))
        except (TimeoutError, socket.timeout):
            raise ProbeError("Desktop IPC connection timed out") from None
        except OSError as exc:
            raise ProbeError(f"Desktop IPC connection failed: {exc.strerror or type(exc).__name__}") from None
        if validate_target(socket_path) != identity:
            raise ProbeError("Desktop IPC socket changed during connection")

        client_id = _send_request(
            connection,
            request_id=str(uuid4()),
            source_client_id=INITIAL_CLIENT_ID,
            method="initialize",
            version=0,
            params={"clientType": "local-ai-connector-probe"},
            deadline=deadline,
        ).get("result")
        if not isinstance(client_id, dict) or not isinstance(client_id.get("clientId"), str) or not client_id["clientId"]:
            raise ProbeError("Desktop IPC initialize returned no client ID")

        discovery = _send_request(
            connection,
            request_id=str(uuid4()),
            source_client_id=client_id["clientId"],
            method="thread-owner-discovery",
            version=1,
            params={"hostId": "local", "conversationId": thread_id},
            deadline=deadline,
        )
        if not isinstance(discovery.get("handledByClientId"), str) or not discovery["handledByClientId"]:
            raise ProbeError("Desktop IPC found no owner for the requested thread", kind="no_owner")
        return {"ok": True, "thread_id": thread_id, "owner_found": True}
    finally:
        if connection is not None:
            connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Codex Desktop thread-owner probe")
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--socket", type=Path, default=Path.home() / ".codex" / "ipc" / "ipc.sock")
    args = parser.parse_args(argv)
    try:
        result = probe(args.thread_id, args.socket)
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
