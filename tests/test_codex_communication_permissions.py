import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "integrations/codex_desktop"))
from permission_migration import (
    ANTIGRAVITY_TOOLS,
    WORKER_TOOLS,
    MigrationConflict,
    migrate_antigravity,
    migrate_codex_global,
    migrate_codex_worker,
    validate_stdio_server,
)


GLOBAL = '''[mcp_servers.local_ai_connector]
required = true
tool_timeout_sec = 660
enabled_tools = ["connector_status", "connector_delegate"]

[mcp_servers.local_ai_connector.tools.connector_status]
approval_mode = "approve"

[mcp_servers.local_ai_connector.tools.connector_delegate]
approval_mode = "approve"

[mcp_servers.other]
enabled_tools = ["leave_this_alone"]
'''

WORKER = '''[mcp_servers.local_ai_connector_codex_desktop_worker]
command = "python"
args = ["--config", "private-endpoint.json"]
'''


def test_global_continue_is_exposed_with_only_its_tool_permission_override():
    migrated, changes = migrate_codex_global(GLOBAL)
    config = tomllib.loads(migrated)
    server = config["mcp_servers"]["local_ai_connector"]

    assert server["enabled_tools"] == ["connector_status", "connector_delegate", "connector_continue"]
    assert server["tools"]["connector_status"]["approval_mode"] == "approve"
    assert server["tools"]["connector_delegate"]["approval_mode"] == "approve"
    assert server["tools"]["connector_continue"]["approval_mode"] == "approve"
    assert config["mcp_servers"]["other"]["enabled_tools"] == ["leave_this_alone"]
    assert len(changes) == 2
    assert migrate_codex_global(migrated) == (migrated, [])


def test_global_conflicting_tool_permission_is_refused_without_erasing_it():
    source = GLOBAL + '\n[mcp_servers.local_ai_connector.tools.connector_continue]\napproval_mode = "prompt"\n'
    with pytest.raises(MigrationConflict, match="conflicting global approval_mode"):
        migrate_codex_global(source)
    assert 'approval_mode = "prompt"' in source


def test_worker_only_auto_approves_status_receive_send_finish():
    migrated, changes = migrate_codex_worker(WORKER)
    tools = tomllib.loads(migrated)["mcp_servers"]["local_ai_connector_codex_desktop_worker"]["tools"]

    assert {name for name, item in tools.items() if item["approval_mode"] == "approve"} == set(WORKER_TOOLS)
    assert "connector_delegate" not in tools
    assert "connector_continue" not in tools
    assert len(changes) == 4
    assert migrate_codex_worker(migrated) == (migrated, [])


def test_worker_allowlist_or_denylist_conflicts_are_refused():
    allowlist = WORKER.replace('args = ["--config", "private-endpoint.json"]',
                               'args = ["--config", "private-endpoint.json"]\nenabled_tools = ["connector_receive"]')
    with pytest.raises(MigrationConflict, match="enabled_tools"):
        migrate_codex_worker(allowlist)
    denylist = WORKER.replace('args = ["--config", "private-endpoint.json"]',
                              'args = ["--config", "private-endpoint.json"]\ndisabled_tools = ["connector_send"]')
    with pytest.raises(MigrationConflict, match="disabled_tools"):
        migrate_codex_worker(denylist)


def test_runtime_mcp_identity_requires_exact_command_endpoint_and_environment():
    server = {
        "command": "/connector/.venv/bin/python",
        "args": ["-m", "local_ai_connector.cli", "mcp", "--config", "/private/codex_desktop.json"],
        "env": {"PYTHONPATH": "/connector/src", "PYTHONDONTWRITEBYTECODE": "1"},
    }
    validate_stdio_server(
        server,
        command="/connector/.venv/bin/python",
        args=["-m", "local_ai_connector.cli", "mcp", "--config", "/private/codex_desktop.json"],
        env={"PYTHONPATH": "/connector/src"},
    )
    wrong_endpoint = {**server, "args": [*server["args"][:-1], "/private/other.json"]}
    with pytest.raises(MigrationConflict, match="different command or endpoint"):
        validate_stdio_server(
            wrong_endpoint,
            command="/connector/.venv/bin/python",
            args=["-m", "local_ai_connector.cli", "mcp", "--config", "/private/codex_desktop.json"],
            env={"PYTHONPATH": "/connector/src"},
        )
    with pytest.raises(MigrationConflict, match="runtime environment"):
        validate_stdio_server(
            server,
            command="/connector/.venv/bin/python",
            args=server["args"],
            env={"PYTHONPATH": "/other/source"},
        )


def test_antigravity_grants_are_exact_and_preserve_other_allow_ask_deny_entries():
    before = {
        "permissionGrants": {
            "v2Migrated": True,
            "permissionGrants": {
                "allow": ["mcp(local_ai_connector/connector_receive)", "mcp(local_ai_connector/connector_send)", "mcp(other/status)"],
                "ask": ["mcp(other/write)"],
                "deny": ["mcp(other/delete)"],
            },
        },
    }
    migrated, changes = migrate_antigravity(before)
    grants = migrated["permissionGrants"]["permissionGrants"]

    assert len(changes) == 4
    assert set(grants["allow"]) == {
        "mcp(local_ai_connector/connector_receive)",
        "mcp(local_ai_connector/connector_send)",
        "mcp(other/status)",
        *(f"mcp(local_ai_connector/{name})" for name in ANTIGRAVITY_TOOLS),
    }
    assert grants["ask"] == before["permissionGrants"]["permissionGrants"]["ask"]
    assert grants["deny"] == before["permissionGrants"]["permissionGrants"]["deny"]
    assert before["permissionGrants"]["permissionGrants"]["allow"] == [
        "mcp(local_ai_connector/connector_receive)", "mcp(local_ai_connector/connector_send)", "mcp(other/status)"
    ]
    assert migrate_antigravity(migrated) == (migrated, [])


@pytest.mark.parametrize("bucket", ["ask", "deny"])
def test_antigravity_conflicting_explicit_permission_is_refused(bucket):
    config = {
        "permissionGrants": {
            "v2Migrated": True,
            "permissionGrants": {"allow": [], bucket: ["mcp(local_ai_connector/connector_delegate)"]},
        },
    }
    with pytest.raises(MigrationConflict, match=bucket):
        migrate_antigravity(config)
