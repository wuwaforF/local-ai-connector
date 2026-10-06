"""Create a disposable Antigravity workspace whose own `.agents/` config adds the probe server and hooks.

Nothing outside the workspace is written: Antigravity loads `.agents/mcp_config.json` and
`.agents/hooks.json` only for this workspace, so global host configuration and any existing
connector installation are untouched. Delete the workspace to remove everything.
"""
import argparse
import json
from pathlib import Path
import shlex
import sys

HERE = Path(__file__).resolve().parent
# MCP calls reach hooks as `mcp_<server>_<tool>` or as `call_mcp_tool` (community-observed); match both.
MATCHER = "call_mcp_tool|mcp_.*"


def build(workspace: Path, python: str = sys.executable, *, extra_servers: dict | None = None,
          deny_server: str | None = None):
    agents, logs = workspace / ".agents", workspace / ".probe"
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(exist_ok=True)
    servers = {"identity_probe": {"command": python,
                                  "args": [str(HERE / "probe_server.py"), "--log", str(logs / "mcp.jsonl")]}}
    (agents / "mcp_config.json").write_text(json.dumps({"mcpServers": {**servers, **(extra_servers or {})}}, indent=2) + "\n")
    guard = ("--deny-server", deny_server) if deny_server else ()

    def hook(event):
        return {"type": "command", "timeout": 10, "command": " ".join(shlex.quote(part) for part in (
            python, str(HERE / "hook.py"), event, str(logs / "hooks.jsonl"), *guard))}
    (agents / "hooks.json").write_text(json.dumps({"connector-identity-probe": {
        "PreToolUse": [{"matcher": MATCHER, "hooks": [hook("pre")]}],
        "PostToolUse": [{"matcher": MATCHER, "hooks": [hook("post")]}],
        "PreInvocation": [hook("invocation")],
    }}, indent=2) + "\n")
    return agents, logs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    agents, logs = build(args.workspace.expanduser().resolve())
    print(f"Workspace ready: {agents.parent}\nLogs: {logs}")
