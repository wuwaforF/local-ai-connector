from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import secrets
import stat

from . import os_adapter
from .core import ConnectorError, require


def _require_safe_platform():
    # The guarantees below rest on O_NOFOLLOW and a dir_fd chain; no equivalent is verified elsewhere.
    require(os_adapter.supports("safe_workspace_write"), "unsupported_platform",
            "Controlled workspace writes are not supported on this platform")


@contextmanager
def write_lease(root: Path):
    """All connector writes to a workspace serialize on a permanent lock inode."""
    _require_safe_platform()
    import fcntl
    root = root.resolve(strict=True)
    fd=os.open(root/".connector-write.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConnectorError("file_busy","工作区正在被另一受控写入占用，请稍后重试")
        yield
    finally:
        os.close(fd)


@contextmanager
def parent_fd(root: Path, relative: str):
    _require_safe_platform()
    parts=relative.split("/")
    require(parts and all(p and p not in (".","..") and not p.startswith(".") and "\\" not in p and "\0" not in p for p in parts), "invalid_path", "须使用工作区内的可见相对文件路径")
    fd=os.open(root.resolve(strict=True),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd)
            fd=next_fd
        yield fd,parts[-1]
    finally:
        os.close(fd)


def snapshot_at(fd, name):
    try:
        file_fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file_fd,"rb") as file:
        info=os.fstat(file.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1, "invalid_file", "只支持普通独立文件")
        require(info.st_size<=10*1024*1024,"file_too_large","首版支持 10 MiB 以内的文件")
        return hashlib.sha256(file.read()).hexdigest()


def snapshot(root: Path, relative: str):
    with parent_fd(root,relative) as (fd,name):
        return snapshot_at(fd,name)


def write_file(root: Path, relative: str, content: bytes, expected_sha256: str | None):
    require(len(content)<=10*1024*1024,"file_too_large","首版支持 10 MiB 以内的文件")
    with write_lease(root), parent_fd(root,relative) as (fd,name):
        require(snapshot_at(fd,name)==expected_sha256,"version_conflict","文件版本已经变化，请重新读取并整合修改")
        temp=".connector-"+secrets.token_hex(8)
        temp_fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd)
        try:
            with os.fdopen(temp_fd,"wb") as file:
                if expected_sha256 is not None:
                    os.fchmod(file.fileno(),stat.S_IMODE(os.stat(name,dir_fd=fd,follow_symlinks=False).st_mode))
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            # Recheck changes made during preparation. Native editors do not honor our lock.
            require(snapshot_at(fd,name)==expected_sha256,"version_conflict","准备写入时文件版本发生变化")
            os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temp,dir_fd=fd)
            except FileNotFoundError:
                pass
    return hashlib.sha256(content).hexdigest()
