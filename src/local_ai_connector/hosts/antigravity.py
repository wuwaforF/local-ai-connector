"""Google Antigravity (~/.gemini/config/mcp_config.json; no command-line installer)."""
from __future__ import annotations

import json
from pathlib import Path

from . import HostConfigError, replace_text

NAME = "antigravity"
DISPLAY_NAME = "Antigravity"
# Antigravity adds the calling conversation to each tool call's request metadata (observed on
# 2.19.1, matching the documented hook payload's conversationId). One MCP process serves all
# chats, so the identity is read per call.
ENDPOINT = {"approval_transport": "elicitation",
            "chat_identity": {"source": "meta", "path": ["antigravity.google/conversation_id"], "namespace": "antigravity"}}
OWNED_KEYS = ("command", "args")
PERMISSIONS = (
    "Refresh MCP servers in Antigravity (Settings > Customizations > MCP Servers) so it loads the new entry.",
    "Antigravity asks before each MCP tool call by default (Ask). Allow rules for the connector's tools are your "
    "choice; setup does not grant them. Task approval is a separate form shown for every new task.",
)
LIMITATIONS = (
    "Chat identity comes from request metadata Antigravity adds to every MCP tool call "
    "(antigravity.google/conversation_id). It is observed on Antigravity 2.19.1 but not publicly documented; "
    "binding reports missing_session_identity if a host version stops sending it.",
    "Automatic wake-up of an Antigravity chat is not available yet; the bound chat collects tasks in attended mode.",
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
