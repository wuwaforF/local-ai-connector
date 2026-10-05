"""Build the fixed local configs for the owner-installed Codex Desktop worker."""

from pathlib import Path
import json


MCP_SERVER_NAME = "local_ai_connector_codex_desktop_worker"


def wake_binding(root: Path, target: dict) -> dict:
    return {
        "adapter": "command",
        "target": target,
        "timeout": 30,
        "command": [
            str(root / ".venv/bin/python"),
            str(root / "integrations/codex_desktop/bridge.py"),
        ],
    }


def project_mcp_config(root: Path, endpoint_path: Path) -> str:
    return (
        f"[mcp_servers.{MCP_SERVER_NAME}]\n"
        f"command = {json.dumps(str(root / '.venv/bin/python'))}\n"
        f"args = [\"-m\", \"local_ai_connector.cli\", \"mcp\", \"--config\", {json.dumps(str(endpoint_path))}]\n"
        f"[mcp_servers.{MCP_SERVER_NAME}.env]\n"
        f"PYTHONPATH = {json.dumps(str(root / 'src'))}\n"
        'PYTHONDONTWRITEBYTECODE = "1"\n'
    )
