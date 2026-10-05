"""Narrow, reviewable Codex and Antigravity tool-permission config changes."""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import tomllib


CODEX_CONTINUE = "connector_continue"
WORKER_TOOLS = ("connector_status", "connector_receive", "connector_send", "connector_finish")
ANTIGRAVITY_TOOLS = ("connector_status", "connector_delegate", "connector_continue", "connector_finish")


class MigrationConflict(ValueError):
    pass


def validate_stdio_server(server: dict, *, command: str, args: list[str], env: dict[str, str]) -> None:
    if not isinstance(server, dict) or server.get("command") != command or server.get("args") != args:
        raise MigrationConflict("The named MCP server points to a different command or endpoint config")
    configured_env = server.get("env")
    if not isinstance(configured_env, dict) or any(configured_env.get(key) != value for key, value in env.items()):
        raise MigrationConflict("The named MCP server has a different runtime environment")


def _table_section(source: str, table: str) -> tuple[int, int, str]:
    header = re.compile(rf"(?m)^\[{re.escape(table)}\][ \t]*(?:\#[^\n]*)?$")
    matches = list(header.finditer(source))
    if len(matches) != 1:
        raise MigrationConflict(f"Expected one TOML table: {table}")
    start = matches[0].start()
    body_start = matches[0].end()
    end_match = re.search(r"(?m)^\[{1,2}.*?\]{1,2}[ \t]*(?:\#[^\n]*)?$", source[body_start:])
    end = body_start + end_match.start() if end_match else len(source)
    return start, end, source[start:end]


def _replace_enabled_tools(source: str, tools: list[str]) -> str:
    table = "mcp_servers.local_ai_connector"
    start, end, section = _table_section(source, table)
    pattern = re.compile(r"(?m)^([ \t]*enabled_tools[ \t]*=[ \t]*)(\[[^\]\n]*\])([ \t]*(?:\#[^\n]*)?)$")
    matches = list(pattern.finditer(section))
    if len(matches) != 1:
        raise MigrationConflict("The connector enabled_tools setting is missing or has an unsupported layout")
    match = matches[0]
    if "#" in match.group(2):
        raise MigrationConflict("The connector enabled_tools list contains a comment; review it manually")
    replacement = match.group(1) + json.dumps(tools) + match.group(3)
    return source[:start] + section[:match.start()] + replacement + section[match.end():] + source[end:]


def _append_tool_overrides(source: str, server_name: str, tool_names: tuple[str, ...]) -> tuple[str, list[str]]:
    document = tomllib.loads(source)
    server = document.get("mcp_servers", {}).get(server_name)
    if not isinstance(server, dict):
        raise MigrationConflict(f"MCP server is missing: {server_name}")
    enabled = server.get("enabled_tools")
    disabled = server.get("disabled_tools")
    if enabled is not None and (not isinstance(enabled, list) or any(name not in enabled for name in tool_names)):
        raise MigrationConflict(f"An explicit enabled_tools list excludes a requested tool on {server_name}")
    if disabled is not None and (not isinstance(disabled, list) or any(name in disabled for name in tool_names)):
        raise MigrationConflict(f"An explicit disabled_tools list conflicts with the requested tools on {server_name}")
    tool_configs = server.get("tools", {})
    if not isinstance(tool_configs, dict):
        raise MigrationConflict(f"Per-tool settings are malformed on {server_name}")
    pending = []
    for name in tool_names:
        current = tool_configs.get(name, {})
        if not isinstance(current, dict):
            raise MigrationConflict(f"Per-tool settings are malformed for {server_name}/{name}")
        if current.get("enabled") is False:
            raise MigrationConflict(f"The tool is explicitly disabled: {server_name}/{name}")
        mode = current.get("approval_mode")
        if mode not in (None, "approve"):
            raise MigrationConflict(f"The tool already has a conflicting approval_mode: {server_name}/{name}")
        if mode is None:
            pending.append(name)
    for name in pending:
        table = f"mcp_servers.{server_name}.tools.{name}"
        table_header = f"[{table}]"
        existing = re.compile(rf"(?m)^\[{re.escape(table)}\][ \t]*(?:\#[^\n]*)?$")
        matches = list(existing.finditer(source))
        if matches:
            if len(matches) != 1:
                raise MigrationConflict(f"Expected one TOML table: {table}")
            at = matches[0].end()
            source = source[:at] + '\napproval_mode = "approve"' + source[at:]
        else:
            if source and not source.endswith("\n"):
                source += "\n"
            source += f"\n{table_header}\napproval_mode = \"approve\"\n"
    # Parse the actual result before returning it; this catches duplicate tables and bad insertion points.
    verified = tomllib.loads(source).get("mcp_servers", {}).get(server_name, {})
    for name in tool_names:
        if verified.get("tools", {}).get(name, {}).get("approval_mode") != "approve":
            raise MigrationConflict(f"Could not verify approval_mode for {server_name}/{name}")
    return source, pending


def migrate_codex_global(source: str) -> tuple[str, list[str]]:
    document = tomllib.loads(source)
    server = document.get("mcp_servers", {}).get("local_ai_connector")
    if not isinstance(server, dict):
        raise MigrationConflict("The global local_ai_connector MCP server is missing")
    enabled = server.get("enabled_tools")
    if not isinstance(enabled, list) or any(not isinstance(name, str) for name in enabled):
        raise MigrationConflict("The global connector enabled_tools list is missing or malformed")
    if len(enabled) != len(set(enabled)):
        raise MigrationConflict("The global connector enabled_tools list contains duplicates")
    disabled = server.get("disabled_tools", [])
    if not isinstance(disabled, list) or CODEX_CONTINUE in disabled:
        raise MigrationConflict("An explicit disabled_tools entry conflicts with connector_continue")
    current_tool = server.get("tools", {}).get(CODEX_CONTINUE, {})
    if not isinstance(current_tool, dict) or current_tool.get("enabled") is False:
        raise MigrationConflict("connector_continue is explicitly disabled in the global config")
    mode = current_tool.get("approval_mode")
    if mode not in (None, "approve"):
        raise MigrationConflict("connector_continue has a conflicting global approval_mode")

    changes = []
    if CODEX_CONTINUE not in enabled:
        enabled = [*enabled, CODEX_CONTINUE]
        source = _replace_enabled_tools(source, enabled)
        changes.append("mcp_servers.local_ai_connector.enabled_tools += connector_continue")
    source, approval_changes = _append_tool_overrides(source, "local_ai_connector", (CODEX_CONTINUE,))
    changes.extend(f"mcp_servers.local_ai_connector.tools.{name}.approval_mode = approve" for name in approval_changes)
    return source, changes


def migrate_codex_worker(source: str) -> tuple[str, list[str]]:
    result, changed = _append_tool_overrides(source, "local_ai_connector_codex_desktop_worker", WORKER_TOOLS)
    changes = [
        f"mcp_servers.local_ai_connector_codex_desktop_worker.tools.{name}.approval_mode = approve"
        for name in changed
    ]
    return result, changes


def migrate_antigravity(source: dict, *, tool_names=ANTIGRAVITY_TOOLS) -> tuple[dict, list[str]]:
    result = copy.deepcopy(source)
    wrapper = result.get("permissionGrants")
    if not isinstance(wrapper, dict) or wrapper.get("v2Migrated") is not True:
        raise MigrationConflict("Antigravity project grants are missing or have an unexpected schema")
    grants = wrapper.get("permissionGrants")
    if not isinstance(grants, dict):
        raise MigrationConflict("Antigravity project permissionGrants is malformed")
    allow = grants.get("allow", [])
    ask = grants.get("ask", [])
    deny = grants.get("deny", [])
    if any(not isinstance(items, list) or any(not isinstance(item, str) for item in items)
           for items in (allow, ask, deny)):
        raise MigrationConflict("Antigravity allow/ask/deny grants must be string arrays")

    requested = [f"mcp(local_ai_connector/{name})" for name in tool_names]
    for bucket_name, bucket in (("ask", ask), ("deny", deny)):
        conflicts = [grant for grant in bucket for tool in requested if fnmatch.fnmatchcase(tool, grant)]
        if conflicts:
            raise MigrationConflict(f"Antigravity {bucket_name} contains a matching grant; no policy was removed")
    missing = [permission for permission in requested if permission not in allow]
    if missing:
        grants["allow"] = [*allow, *missing]
    return result, [f"permissionGrants.permissionGrants.allow += {permission}" for permission in missing]
