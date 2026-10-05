"""Windows implementation: msvcrt byte-range locks, DACL ownership checks, os.replace.

Not provided here (reported as unsupported): Unix-socket bridges and the race-resistant
workspace write in files.py, whose POSIX guarantees rely on O_NOFOLLOW and dir_fd. A check
for reparse points followed by a replace is not equivalent, so it is not offered.
"""
from __future__ import annotations

from contextlib import contextmanager
import msvcrt
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

import ntsecuritycon as con
import win32api
import win32security

NAME = "windows"
FEATURES = frozenset()

_ALLOWED_ACE = win32security.ACCESS_ALLOWED_ACE_TYPE
_INHERIT_ONLY = win32security.INHERIT_ONLY_ACE
_WRITE_MASK = (con.FILE_WRITE_DATA | con.FILE_APPEND_DATA | con.FILE_WRITE_EA | con.FILE_WRITE_ATTRIBUTES
               | con.WRITE_DAC | con.WRITE_OWNER | con.DELETE | con.GENERIC_WRITE | con.GENERIC_ALL)
_SYSTEM = win32security.ConvertStringSidToSid("S-1-5-18")
_ADMINISTRATORS = win32security.ConvertStringSidToSid("S-1-5-32-544")


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
    if descriptor.GetSecurityDescriptorOwner() not in trusted:
        raise NotPrivate(f"{path} is not owned by the current user")
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        raise NotPrivate(f"{path} has no access control list (everyone has access)")
    for index in range(dacl.GetAceCount()):
        (ace_type, ace_flags), mask, sid = dacl.GetAce(index)[:3]
        if ace_type != _ALLOWED_ACE or ace_flags & _INHERIT_ONLY or sid in trusted:
            continue
        if not writable_only or mask & _WRITE_MASK:
            raise NotPrivate(f"{path} grants access to another principal")


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
    """Create a new file; it inherits the private directory's DACL and is then verified."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY | os.O_NOINHERIT, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    check_private(path)


def replace_private_file(path: Path, data: bytes):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
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
