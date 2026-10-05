"""Operating-system adapter: the only place that branches on the platform.

Core modules call these functions instead of POSIX- or Windows-specific APIs. Only the
implementation for the running platform is imported. A capability that has not been verified
on a platform is reported as unsupported there instead of being emulated.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

if sys.platform == "win32":
    from . import windows as _impl
else:
    from . import posix as _impl

NAME = _impl.NAME
APP_NAME = "Local AI Connector"


class Unsupported(RuntimeError):
    """A capability this platform's adapter does not provide."""

    def __init__(self, feature: str):
        super().__init__(f"{feature} is not supported on this platform ({NAME})")
        self.feature = feature


# Re-exported primitives. Each implementation module provides the same names.
LockBusy = _impl.LockBusy
NotPrivate = _impl.NotPrivate
exclusive_lock = _impl.exclusive_lock
ensure_private_dir = _impl.ensure_private_dir
create_private_file = _impl.create_private_file
replace_private_file = _impl.replace_private_file
check_private = _impl.check_private
spawn_detached = _impl.spawn_detached
FEATURES = _impl.FEATURES


def supports(feature: str) -> bool:
    return feature in FEATURES


def require(feature: str):
    if feature not in FEATURES:
        raise Unsupported(feature)


def is_private(path: Path, *, writable_only: bool = False) -> bool:
    try:
        check_private(path, writable_only=writable_only)
    except (NotPrivate, OSError):
        return False
    return True


def app_data_dir(*, platform: str = sys.platform, environ=os.environ, home: Path | None = None) -> Path:
    """Per-user application data directory; distinct from the legacy helper location on macOS."""
    home = Path(home) if home is not None else Path.home()
    if platform == "win32":
        base = environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / APP_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    xdg = environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".local" / "share"
    return base / "local-ai-connector"
