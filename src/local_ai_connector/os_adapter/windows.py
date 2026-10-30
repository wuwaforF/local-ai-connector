"""Windows implementation: msvcrt byte-range locks, DACL ownership checks, os.replace.

Not provided here (reported as unsupported): Unix-socket bridges and the race-resistant
workspace write in files.py, whose POSIX guarantees rely on O_NOFOLLOW and dir_fd. A check
for reparse points followed by a replace is not equivalent, so it is not offered.

Local IPC is a named-pipe client (connect_local_ipc) whose server is identified through the open
connection (endpoint_identity); both wake bridges use it.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import msvcrt
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

import ntsecuritycon as con
import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32process
import win32security

NAME = "windows"
FEATURES = frozenset({"local_ipc"})
_STILL_ACTIVE = 259
_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND, _ERROR_ACCESS_DENIED = 2, 3, 5
_ERROR_BROKEN_PIPE, _ERROR_PIPE_BUSY, _ERROR_MORE_DATA, _ERROR_IO_PENDING = 109, 231, 234, 997
_ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED = 232, 233
# How a client sees the server end its side: it closed its handle (109) or called DisconnectNamedPipe (233).
_SERVER_CLOSED = frozenset({_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED})
# A write can also meet a pipe that is still being closed (232).
_WRITE_CLOSED = _SERVER_CLOSED | {_ERROR_NO_DATA}

_ALLOWED_ACE = win32security.ACCESS_ALLOWED_ACE_TYPE
_INHERIT_ONLY = win32security.INHERIT_ONLY_ACE
_WRITE_MASK = (con.FILE_WRITE_DATA | con.FILE_APPEND_DATA | con.FILE_WRITE_EA | con.FILE_WRITE_ATTRIBUTES
               | con.WRITE_DAC | con.WRITE_OWNER | con.DELETE | con.GENERIC_WRITE | con.GENERIC_ALL)
_SYSTEM = win32security.ConvertStringSidToSid("S-1-5-18")
_ADMINISTRATORS = win32security.ConvertStringSidToSid("S-1-5-32-544")
# OWNER RIGHTS stands for whoever owns the object; the owner itself is checked separately.
# CPython uses it for directories created with mode 0o700.
_OWNER_RIGHTS = win32security.ConvertStringSidToSid("S-1-3-4")


class LockBusy(BlockingIOError):
    pass


class NotPrivate(PermissionError):
    pass


def _current_user():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = False):
    """Exclusive lock on the first byte; also excludes other handles in this process."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_BINARY | os.O_NOINHERIT, 0o600)
    try:
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if not blocking:
                    raise LockBusy(f"{path} is locked by another process") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _reparse(path: Path) -> bool:
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def check_private(path: Path, *, writable_only: bool = False):
    """Owner is this user (or a privileged principal) and no other principal has access."""
    if _reparse(path):
        raise NotPrivate(f"{path} is a reparse point")
    descriptor = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
    user = _current_user()
    trusted = (user, _SYSTEM, _ADMINISTRATORS)
    owner = descriptor.GetSecurityDescriptorOwner()
    if owner not in trusted:
        raise NotPrivate(f"{path} is owned by {win32security.ConvertSidToStringSid(owner)}, not the current user")
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        raise NotPrivate(f"{path} has no access control list (everyone has access)")
    for index in range(dacl.GetAceCount()):
        (ace_type, ace_flags), mask, sid = dacl.GetAce(index)[:3]
        if ace_type != _ALLOWED_ACE or ace_flags & _INHERIT_ONLY or sid in trusted or sid == _OWNER_RIGHTS:
            continue
        if not writable_only or mask & _WRITE_MASK:
            raise NotPrivate(f"{path} grants access to {win32security.ConvertSidToStringSid(sid)}")


def _protect(path: Path, *, directory: bool):
    """Replace inherited access with this user and SYSTEM only."""
    inherit = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE if directory else 0
    dacl = win32security.ACL()
    for sid in (_current_user(), _SYSTEM):
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inherit, con.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, dacl, None)


def ensure_private_dir(path: Path):
    """Create path if needed; a directory created here gets a protected, inheritable DACL."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
    except FileExistsError:
        pass
    else:
        _protect(path, directory=True)
    check_private(path)


def create_private_file(path: Path, data: bytes):
    """Create a new file with a protected owner-only DACL, set before content is written."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY | os.O_NOINHERIT, 0o600)
    try:
        _protect(path, directory=False)  # before any content is written
        with os.fdopen(fd, "wb") as file:
            fd = None
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    finally:
        if fd is not None:
            os.close(fd)
    check_private(path)


def replace_private_file(path: Path, data: bytes):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        _protect(Path(temporary), directory=False)  # before any content is written
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        check_private(Path(temporary))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def spawn_detached(argv: list[str], *, log_path: Path, cwd: Path | None = None) -> int:
    """Start without a console in a new process group; leave the caller's job when allowed."""
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log_path, "ab") as log:
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=cwd,
                                       close_fds=True, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
        except OSError:
            # The host's job object does not permit breakaway; the service then shares its lifetime.
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=cwd,
                                       close_fds=True, creationflags=flags)
    return process.pid


def read_owned_text(path: Path, *, private: bool = False) -> str:
    """A regular file owned by this user that others cannot write (or, if private, access), as UTF-8."""
    check_private(path, writable_only=not private)  # also refuses reparse points
    with open(path, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise NotPrivate(f"{path} is not a regular file")
        return file.read().decode("utf-8")


def fsync_dir(path: Path):
    """NTFS journals directory entries; there is no directory handle to flush here."""


def _open_process(pid):
    return win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)


def _process_user(handle):
    token = win32security.OpenProcessToken(handle, win32security.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def own_process_alive(pid) -> bool:
    """True if pid is a running process of this user. Never signals it: os.kill(pid, 0) on
    Windows would send CTRL_C_EVENT."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        handle = _open_process(pid)
    except pywintypes.error:
        return False
    try:
        return win32process.GetExitCodeProcess(handle) == _STILL_ACTIVE and _process_user(handle) == _current_user()
    except pywintypes.error:
        return False
    finally:
        handle.Close()


def process_start_time(pid) -> float | None:
    try:
        handle = _open_process(int(pid))
    except (pywintypes.error, ValueError, TypeError):
        return None
    try:
        return win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
    except (pywintypes.error, KeyError, AttributeError, ValueError, OSError):
        return None
    finally:
        handle.Close()


def _os_error(exc, path):
    code = exc.winerror
    if code in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
        return FileNotFoundError(errno.ENOENT, exc.strerror, str(path))
    if code == _ERROR_ACCESS_DENIED:
        return PermissionError(errno.EACCES, exc.strerror, str(path))
    if code == _ERROR_PIPE_BUSY:
        return TimeoutError(errno.ETIMEDOUT, "The named pipe stayed busy", str(path))
    return ConnectionError(errno.ECONNREFUSED, exc.strerror, str(path))


class NamedPipe:
    """A client named-pipe connection with the socket methods the wake bridges use."""

    def __init__(self, handle, timeout):
        self._handle, self._timeout = handle, timeout
        self._event = win32event.CreateEvent(None, True, False, None)

    def settimeout(self, timeout):
        self._timeout = timeout

    def _complete(self, overlapped, operation):
        wait = win32event.INFINITE if self._timeout is None else max(0, int(self._timeout * 1000))
        if win32event.WaitForSingleObject(self._event, wait) != win32event.WAIT_OBJECT_0:
            win32file.CancelIo(self._handle)
            try:
                win32file.GetOverlappedResult(self._handle, overlapped, True)
            except pywintypes.error:
                pass
            raise TimeoutError(errno.ETIMEDOUT, f"Named pipe {operation} timed out")
        try:
            return win32file.GetOverlappedResult(self._handle, overlapped, False)
        except pywintypes.error as exc:
            if exc.winerror == _ERROR_MORE_DATA:
                return None  # a message-mode read filled the buffer; the rest follows
            raise

    def _overlapped(self):
        win32event.ResetEvent(self._event)
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = self._event
        return overlapped

    def sendall(self, data):
        view = memoryview(bytes(data))
        while view:
            overlapped = self._overlapped()
            chunk = view.tobytes()  # stays referenced until the overlapped write completes
            try:
                win32file.WriteFile(self._handle, chunk, overlapped)
                written = self._complete(overlapped, "write")
            except pywintypes.error as exc:
                if exc.winerror in _WRITE_CLOSED:
                    raise BrokenPipeError(errno.EPIPE, exc.strerror) from None
                raise ConnectionError(errno.ECONNRESET, exc.strerror) from None
            view = view[written:]

    def recv(self, size):
        buffer = win32file.AllocateReadBuffer(size)
        overlapped = self._overlapped()
        try:
            win32file.ReadFile(self._handle, buffer, overlapped)
            count = self._complete(overlapped, "read")
        except pywintypes.error as exc:
            if exc.winerror in _SERVER_CLOSED:
                return b""  # the server ended its side: end of stream, as with a socket
            raise ConnectionError(errno.ECONNRESET, exc.strerror) from None
        return bytes(buffer[: size if count is None else count])

    def shutdown(self, how):
        """A pipe has no half-close; callers frame their messages, and close() ends the stream."""

    def close(self):
        if self._handle is not None:
            self._handle.Close()
            self._event.Close()
            self._handle = None

    def server_process_id(self):
        return win32pipe.GetNamedPipeServerProcessId(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect_local_ipc(path, timeout: float):
    """A stream connection to a local endpoint: here a named pipe, waiting while it is busy."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = win32file.CreateFile(str(path), win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None,
                                          win32file.OPEN_EXISTING, win32file.FILE_FLAG_OVERLAPPED, None)
            return NamedPipe(handle, timeout)
        except pywintypes.error as exc:
            if exc.winerror != _ERROR_PIPE_BUSY or time.monotonic() >= deadline:
                raise _os_error(exc, path) from None
        try:
            win32pipe.WaitNamedPipe(str(path), max(1, int((deadline - time.monotonic()) * 1000)))
        except pywintypes.error:
            pass


def endpoint_identity(path, connection=None) -> tuple:
    """Who serves a named pipe: its server process, which must be a running process of this user.

    A pipe has no file identity, so this needs an open connection to ask for the server's PID.
    """
    if connection is None:
        raise NotPrivate(f"{path}: a named pipe's server can be identified only through a connection")
    try:
        pid = connection.server_process_id()
    except pywintypes.error as exc:
        raise NotPrivate(f"{path}: the pipe server could not be identified ({exc.strerror})") from None
    if not own_process_alive(pid):
        raise NotPrivate(f"{path} is served by a process that is not the current user's")
    return ("pipe", pid, process_start_time(pid))
