"""POSIX implementation (macOS, Linux, WSL): flock, owner/mode checks, O_EXCL and os.replace."""
from __future__ import annotations

import calendar
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import time

NAME = "posix"
# unix_socket_bridge: adapter_socket.py; safe_workspace_write: files.py (O_NOFOLLOW + dir_fd chain);
# local_ipc: connect_local_ipc / endpoint_identity for the wake bridges (Unix sockets).
FEATURES = frozenset({"unix_socket_bridge", "safe_workspace_write", "local_ipc"})


class LockBusy(BlockingIOError):
    pass


class NotPrivate(PermissionError):
    pass


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = False):
    """Hold an exclusive advisory lock on path for the duration of the block."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise LockBusy(f"{path} is locked by another process") from None
        yield
    finally:
        os.close(fd)


def check_private(path: Path, *, writable_only: bool = False):
    """Owned by this user, not a symlink, and no group/other access (or write access)."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise NotPrivate(f"{path} is a symbolic link")
    if info.st_uid != os.getuid():
        raise NotPrivate(f"{path} is not owned by the current user")
    mask = 0o022 if writable_only else 0o077
    if stat.S_IMODE(info.st_mode) & mask:
        raise NotPrivate(f"{path} is accessible to other users")


def ensure_private_dir(path: Path):
    """Create path (and parents) if needed; the leaf must end up private to this user."""
    try:
        path.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        pass
    check_private(path)


def create_private_file(path: Path, data: bytes):
    """Create a new owner-only file; fails if the path exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def replace_private_file(path: Path, data: bytes):
    """Atomically replace path with an owner-only file in the same directory."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def spawn_detached(argv: list[str], *, log_path: Path, cwd: Path | None = None) -> int:
    """Start a process in its own session so it outlives the caller's process group."""
    with open(log_path, "ab") as log:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   cwd=cwd, close_fds=True, start_new_session=True)
    return process.pid


def read_owned_text(path: Path, *, private: bool = False) -> str:
    """A regular file owned by this user that others cannot write (or, if private, access), as UTF-8."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise NotPrivate(f"{path} is not a regular file")
        if info.st_uid != os.getuid():
            raise NotPrivate(f"{path} is not owned by the current user")
        if stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022):
            raise NotPrivate(f"{path} is accessible to other users")
        return file.read().decode("utf-8")


def fsync_dir(path: Path):
    """Make a directory entry change durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def own_process_alive(pid) -> bool:
    """True if pid is a running process of this user. Signal 0 only checks; it delivers nothing."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # it exists, but belongs to another user
        return False
    return True


def process_start_time(pid) -> float | None:
    """When pid started, in seconds since the epoch, or None if that cannot be read."""
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                                timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    try:
        return float(calendar.timegm(time.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return None


def connect_local_ipc(path, timeout: float):
    """A stream connection to a local endpoint: here a Unix socket. Socket methods apply."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(str(path))
    except BaseException:
        connection.close()
        raise
    return connection


def endpoint_identity(path, connection=None) -> tuple:
    """Who serves a local endpoint: an owner-only Unix socket of this user, by its file identity."""
    info = os.lstat(path)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise NotPrivate(f"{path} must be an owner-only socket of the current user, not a symlink")
    return ("socket", info.st_dev, info.st_ino)
