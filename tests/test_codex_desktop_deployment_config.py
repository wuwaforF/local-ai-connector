import json
import sys
import tomllib
from pathlib import Path

from local_ai_connector.wakeup import CommandAdapter, load_config
import pytest

pytestmark = pytest.mark.macos  # macOS deployment scripts, launchd or Desktop paths

sys.path.insert(0, str(Path(__file__).parents[1] / "integrations/codex_desktop"))
from deployment_config import MCP_SERVER_NAME, project_mcp_config, wake_binding


def test_installer_binding_loads_for_exact_codex_thread_and_preserves_existing_binding(tmp_path):
    root = Path("/connector").resolve()
    target = {
        "thread_id": "0190f2c1-7a3b-7c4d-8e5f-6a7b8c9d0e1f",
        "workspace": "/workspaces/dedicated-codex-worker",
    }
    existing = {"adapter": "command", "command": ["/bin/true"], "target": {"session_id": "other"}}
    config = {
        "enabled": True,
        "send_when_unknown": True,
        "bindings": {"existing_worker": existing, "codex_desktop": wake_binding(root, target)},
    }
    (tmp_path / "wakeup.json").write_text(json.dumps(config))

    bindings, options = load_config(tmp_path, {"existing_worker", "codex_desktop"})

    assert options["send_when_unknown"] is True
    assert bindings["existing_worker"].target == existing["target"]
    worker = bindings["codex_desktop"]
    assert isinstance(worker.adapter, CommandAdapter)
    assert worker.adapter.timeout == 30
    assert worker.target == target
    assert worker.adapter.command == [
        str(root / ".venv/bin/python"),
        str(root / "integrations/codex_desktop/bridge.py"),
    ]


def test_installer_project_mcp_config_points_to_private_endpoint_without_credentials(tmp_path):
    root = Path("/connector").resolve()
    endpoint = Path("/private/connector/codex_desktop.json")

    parsed = tomllib.loads(project_mcp_config(root, endpoint))

    server = parsed["mcp_servers"][MCP_SERVER_NAME]
    assert server["command"] == str(root / ".venv/bin/python")
    assert server["args"][-1] == str(endpoint)
    assert server["env"]["PYTHONPATH"] == str(root / "src")
    assert "token" not in json.dumps(parsed).lower()
