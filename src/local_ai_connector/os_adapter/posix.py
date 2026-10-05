"""POSIX implementation (macOS, Linux, WSL): flock, owner/mode checks, O_EXCL and os.replace."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import tempfile

NAME = "posix"
# unix_socket_bridge: adapter_socket.py; safe_workspace_write: files.py (O_NOFOLLOW + dir_fd chain).
FEATURES = frozenset({"unix_socket_bridge", "safe_workspace_write"})


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
