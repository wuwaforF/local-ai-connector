"""Run a command bridge in its host environment, reached over a private local socket."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import stat

from .wakeup import AdapterError, CommandAdapter

LIMIT = 65536


class SocketAdapter(CommandAdapter):
    def __init__(self, path: str, timeout: float = 30):
        self.path, self.timeout = path, timeout

    async def _call(self, op, **fields):
        writer = None
        submitted = False
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(self.path, limit=LIMIT)
                submitted = True
                writer.write(json.dumps({"op": op, **fields}).encode() + b"\n")
                await writer.drain()
                raw = await reader.readline()
                reply = json.loads(raw)
                if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
                    raise ValueError("Invalid bridge reply")
                if not reply["ok"]:
                    kind = reply.get("error")
                    if kind not in ("rejected", "unavailable", "stale_target", "ambiguous"):
                        kind = "ambiguous" if op in ("send", "create", "native_send", "archive") else "malformed"
                    raise AdapterError(kind, str(reply.get("detail", ""))[:200],
                                       retryable=reply.get("retryable") is True)
                return reply
        except (OSError, TimeoutError, ValueError) as exc:
            kind = "ambiguous" if op in ("send", "create", "native_send", "archive") and submitted else "unavailable"
            raise AdapterError(kind, f"local bridge: {type(exc).__name__}") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass  # The operation result above already records connection failures.


async def handle(reader, writer, adapter, native_conversations=False):
    try:
        async with asyncio.timeout(60):
            request = json.loads(await reader.readline())
            operations = ("confirm", "status", "send", "reconcile") + (("create", "native_send", "archive") if native_conversations else ())
            if not isinstance(request, dict) or request.get("op") not in operations:
                raise ValueError("Unsupported operation")
            if not isinstance(request.get("target"), dict):
                raise ValueError("Explicit target required")
            op = request.pop("op")
            allowed = {"target", "dispatch_id", "text"} if op == "send" else {"target", "dispatch_id"} if op == "reconcile" else {"target"}
            if op in ("create", "native_send"):
                allowed = {"target", "dispatch_id", "text", "expires_at"} | ({"title"} if op == "create" else set())
            elif op == "archive":
                allowed = {"target", "expires_at"}
            if set(request) != allowed:
                raise ValueError("Unexpected request fields")
            reply = await adapter._call(op, **request)
    except AdapterError as exc:
        reply = {"ok": False, "error": exc.kind, "detail": exc.detail, "retryable": exc.retryable}
    except (ValueError, TypeError, TimeoutError) as exc:
        # A timeout may follow a side effect, so this bridge must never promise non-delivery.
        reply = {"ok": False, "error": "ambiguous", "detail": type(exc).__name__}
    except asyncio.CancelledError:
        writer.close()
        raise
    try:
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
    except OSError:
        pass  # Client disconnected; its durable dispatch remains uncertain.
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def serve(path: Path, command: list[str], timeout: float, native_conversations=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Socket directory must be owned by this user with mode 0700")
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise ValueError("Refusing to replace a non-socket path")
            path.unlink()
        adapter = CommandAdapter(command, timeout)
        tasks = set()
        def connected(reader, writer):
            task = asyncio.create_task(handle(reader, writer, adapter, native_conversations))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        old_mask = os.umask(0o077)
        try:
            server = await asyncio.start_unix_server(connected, str(path), limit=LIMIT)
            path.chmod(0o600)
        finally:
            os.umask(old_mask)
        owned_inode = path.stat().st_ino
        try:
            async with server:
                await server.serve_forever()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*list(tasks), return_exceptions=True)
            if path.exists() and path.lstat().st_ino == owned_inode:
                path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--native-conversations", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 1 <= args.timeout <= 45:
        parser.error("A bridge command and a timeout between 1 and 45 seconds are required")
    asyncio.run(serve(args.socket.resolve(), command, args.timeout, args.native_conversations))


if __name__ == "__main__":
    main()
