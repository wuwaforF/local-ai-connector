"""Google Antigravity (~/.gemini/config/mcp_config.json; no command-line installer)."""
from __future__ import annotations

import json
from pathlib import Path

from . import HostConfigError, replace_text

NAME = "antigravity"
DISPLAY_NAME = "Antigravity"
# No trusted chat identity reaches MCP servers, so exact-chat binding is not offered.
ENDPOINT = {"approval_transport": "elicitation", "chat_identity": None}
OWNED_KEYS = ("command", "args")
PERMISSIONS = (
    "Refresh MCP servers in Antigravity (Settings > Customizations > MCP Servers) so it loads the new entry.",
    "Antigravity asks before each MCP tool call by default (Ask). Allow rules for the connector's tools are your "
    "choice; setup does not grant them. Task approval is a separate form shown for every new task.",
)
LIMITATIONS = (
    "Antigravity does not tell MCP servers which conversation is calling, so the connector cannot verify the "
    "exact receiving chat. Exact-chat binding and receipt are not supported for Antigravity workers.",
    "Antigravity ends tool calls after about 180 seconds; long tasks started from Antigravity report 'running' "
    "and must be resumed.",
)


def config_path(env) -> Path:
    return env.home / ".gemini" / "config" / "mcp_config.json"


def _document(env) -> dict:
    path = config_path(env)
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HostConfigError("host_config_unreadable", f"Cannot read {path}: {type(exc).__name__}") from None
    if not isinstance(document, dict) or not isinstance(document.get("mcpServers", {}), dict):
        raise HostConfigError("host_config_unreadable", f"Unexpected structure in {path}")
    return document


def launch_entry(command: str, args: list[str]) -> dict:
    return {"command": command, "args": list(args)}


def owned_view(entry: dict) -> dict:
    return {key: entry[key] for key in OWNED_KEYS if key in entry}


def read_entry(name: str, env) -> dict | None:
    return _document(env).get("mcpServers", {}).get(name)


def write_entry(name: str, entry: dict, env):
    document = _document(env)
    servers = document.setdefault("mcpServers", {})
    servers[name] = {**servers.get(name, {}), **entry}  # keep keys the user added
    replace_text(config_path(env), json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def remove_entry(name: str, env):
    document = _document(env)
    if name in document.get("mcpServers", {}):
        del document["mcpServers"][name]
        replace_text(config_path(env), json.dumps(document, indent=2, ensure_ascii=False) + "\n")
