"""Codex (desktop app, CLI and IDE share ~/.codex/config.toml).

`codex mcp add` cannot set timeouts, so the table is edited with tomlkit, which keeps the
user's comments and ordering. Only the launch keys below are owned; per-tool approval
settings the user adds through Codex stay theirs.
"""
from __future__ import annotations

from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from . import HostConfigError, replace_text

NAME = "codex"
DISPLAY_NAME = "Codex"
# Codex sends the calling thread in each tool call's _meta; the form shows the task for approval.
ENDPOINT = {"approval_transport": "elicitation", "chat_identity": {"source": "codex_meta"}}
OWNED_KEYS = ("command", "args", "startup_timeout_sec", "tool_timeout_sec")
PERMISSIONS = (
    "Restart MCP servers in Codex (Settings > MCP servers > Restart) so it loads the new entry.",
    "Codex asks before each connector tool call by default. Allowing the connector's tools in Codex is your "
    "choice; setup does not grant it. Task approval is a separate form shown for every new task.",
    "If your approval policy auto-rejects MCP forms, task approval cannot be shown; allow MCP elicitations.",
)
LIMITATIONS = ("On macOS, setup turns on automatic wake-up of the bound chat (turn it off with setup codex "
               "--no-wake). It needs the chat open and idle in Codex Desktop and uses the desktop's undocumented local "
               "IPC. Elsewhere, or with wake-up off, the bound chat collects tasks when asked.",)
# Wakes the chat pinned at approval through Codex Desktop's local thread IPC (macOS only).
WAKE = {"platforms": ("darwin",), "bridge": ("integrations", "codex_desktop", "bridge.py"),
        "state_dir": "codex-desktop-dispatches"}


def config_path(env) -> Path:
    base = env.environ.get("CODEX_HOME")
    return (Path(base) if base else env.home / ".codex") / "config.toml"


def _document(env):
    path = config_path(env)
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    except (OSError, UnicodeDecodeError, TOMLKitError) as exc:
        raise HostConfigError("host_config_unreadable", f"Cannot read {path}: {type(exc).__name__}") from None


def launch_entry(command: str, args: list[str]) -> dict:
    return {"command": command, "args": list(args), "startup_timeout_sec": 20, "tool_timeout_sec": 660}


def owned_view(entry: dict) -> dict:
    return {key: entry[key] for key in OWNED_KEYS if key in entry}


def read_entry(name: str, env) -> dict | None:
    servers = _document(env).get("mcp_servers")
    if servers is None or name not in servers:
        return None
    return servers[name].unwrap()


def write_entry(name: str, entry: dict, env):
    document = _document(env)
    if "mcp_servers" not in document:
        document["mcp_servers"] = tomlkit.table(is_super_table=True)
    servers = document["mcp_servers"]
    if name in servers:
        for key in OWNED_KEYS:  # keep keys and sub-tables the user added
            servers[name][key] = entry[key]
    else:
        table = tomlkit.table()
        for key in OWNED_KEYS:
            table[key] = entry[key]
        servers[name] = table
    replace_text(config_path(env), tomlkit.dumps(document))


def remove_entry(name: str, env):
    document = _document(env)
    servers = document.get("mcp_servers")
    if servers is not None and name in servers:
        del servers[name]
        replace_text(config_path(env), tomlkit.dumps(document))
