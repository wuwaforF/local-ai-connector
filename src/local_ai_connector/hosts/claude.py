"""Claude Desktop Code tab and Claude Code CLI (user-scope MCP servers in ~/.claude.json).

Claude Code rewrites ~/.claude.json constantly, so writes go through its supported
`claude mcp add-json` / `claude mcp remove` commands; the file is only read here.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from . import HostConfigError

NAME = "claude"
DISPLAY_NAME = "Claude Desktop Code"
# Forms are not shown in Desktop, so each delegate call needs the native permission prompt.
# Each chat runs its own MCP process with its session in CLAUDE_CODE_SESSION_ID.
ENDPOINT = {"approval_transport": "host_tool_permission",
            "chat_identity": {"source": "env", "variable": "CLAUDE_CODE_SESSION_ID", "namespace": "claude"}}
OWNED_KEYS = ("command", "args")
EDITS_CONFIG_DIRECTLY = False  # Claude's own CLI edits ~/.claude.json; nothing to back up here
PERMISSIONS = (
    "Reconnect MCP servers in Claude (/mcp, then reconnect) or start a new chat so it loads the new entry.",
    "connector_delegate always asks for your permission in Claude; that prompt is the task approval. "
    "Never add an allow rule for connector_delegate.",
    "Allowing connector_receive, connector_send and connector_status without prompts is your choice; setup "
    "does not grant it.",
)
LIMITATIONS = ("Automatic wake-up of a Claude chat is not available yet; the bound chat collects tasks in attended mode.",
               "A project's own .mcp.json server with the same name takes precedence inside that project; "
               "setup cannot see those files. Use --profile if you already have one.",
               "Chat identity relies on CLAUDE_CODE_SESSION_ID reaching the connector's MCP process; doctor and "
               "binding report it when the host does not provide it.")
CLI_OVERRIDE = "LOCAL_AI_CONNECTOR_CLAUDE_CLI"


def config_path(env) -> Path:
    base = env.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else env.home) / ".claude.json"


def find_cli(env) -> list[str] | None:
    """The claude CLI: an explicit override, PATH, or the copy bundled with Claude Desktop on macOS."""
    override = env.environ.get(CLI_OVERRIDE)
    if override:
        return json.loads(override) if override.startswith("[") else [override]
    found = shutil.which("claude", path=env.environ.get("PATH"))
    if found:
        return [found]
    bundled = sorted((env.home / "Library/Application Support/Claude/claude-code").glob(
        "*/*/claude.app/Contents/MacOS/claude"))
    return [str(bundled[-1])] if bundled else None


def _run(env, *args):
    cli = find_cli(env)
    if cli is None:
        raise HostConfigError("host_cli_missing", "Claude Code was not found. Open Claude Desktop once (Code tab) or "
                              f"install Claude Code, or set {CLI_OVERRIDE} to the claude executable.")
    environment = {**env.environ, "HOME": str(env.home)}
    result = subprocess.run([*cli, *args], capture_output=True, text=True, timeout=60, env=environment,
                            stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise HostConfigError("host_cli_failed", f"claude {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")


def launch_entry(command: str, args: list[str]) -> dict:
    return {"type": "stdio", "command": command, "args": list(args)}


def owned_view(entry: dict) -> dict:
    return {key: entry[key] for key in OWNED_KEYS if key in entry}


def _document(env) -> dict:
    path = config_path(env)
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HostConfigError("host_config_unreadable", f"Cannot read {path}: {type(exc).__name__}") from None
    if not isinstance(document, dict):
        raise HostConfigError("host_config_unreadable", f"Unexpected structure in {path}")
    return document


def read_entry(name: str, env) -> dict | None:
    return (_document(env).get("mcpServers") or {}).get(name)


def collisions(name: str, env) -> list[str]:
    """Projects whose local-scope server of the same name would shadow the user-scope entry."""
    projects = _document(env).get("projects") or {}
    return sorted(path for path, project in projects.items()
                  if isinstance(project, dict) and name in (project.get("mcpServers") or {}))


def write_entry(name: str, entry: dict, env):
    if read_entry(name, env) is not None:
        _run(env, "mcp", "remove", "--scope", "user", name)
    _run(env, "mcp", "add-json", "--scope", "user", name, json.dumps(entry))


def remove_entry(name: str, env):
    if read_entry(name, env) is not None:
        _run(env, "mcp", "remove", "--scope", "user", name)
