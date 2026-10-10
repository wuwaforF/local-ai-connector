"""Host adapters: what differs between desktop hosts, and nothing that differs between OSes.

Each module provides:
  NAME, DISPLAY_NAME
  ENDPOINT            endpoint settings: approval transport and trusted chat identity (or None)
  config_path(env)    the host's MCP configuration file
  read_entry(name, env) / write_entry(name, entry, env) / remove_entry(name, env)
  launch_entry(command, args) -> the entry this installer owns
  owned_view(entry)   the launch keys the installer owns, used to detect user edits
  PERMISSIONS, LIMITATIONS  user guidance; setup never grants permissions itself
  WAKE (optional)     an opt-in wake-up bridge for the chat pinned at approval: the platforms it
                      supports, its path in a source checkout, and its private state folder
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path


class HostConfigError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HostEnv:
    """Where a host keeps its configuration for this user."""
    home: Path
    environ: dict = field(default_factory=dict)


def fingerprint(view: dict) -> str:
    return hashlib.sha256(json.dumps(view, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def replace_text(path: Path, text: str):
    """Atomically replace a host-owned text file, keeping its permission bits where POSIX has them."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        if mode is not None and os.name == "posix":
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# Imported last: the host modules use the helpers above.
from . import antigravity, claude, codex  # noqa: E402

HOSTS = {module.NAME: module for module in (codex, claude, antigravity)}
