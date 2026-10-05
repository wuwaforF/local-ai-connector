"""Credential-free native tool-permission probe; never dispatches tasks."""
import argparse
import json
import os
import time
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations


def create_probe(evidence: Path):
    def record(event, **fields):
        fd = os.open(evidence, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as output:
            output.write(json.dumps({"time": time.time(), "event": event, **fields}) + "\n")

    async def observe(ctx, call_next):
        if ctx.method in ("initialize", "server/discover", "tools/list"):
            record("protocol_request", method=ctx.method)
        return await call_next(ctx)

    server = MCPServer(
        "Isolated Tool Permission Probe",
        instructions="Only run the exact probe requested by the user. Let the human handle permission prompts. This diagnostic never sends tasks or grants access. Report actual results and stop; never retry on denial.",
        middleware=[observe],
    )

    @server.tool(
        meta={"anthropic/requiresUserInteraction": True},
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
    )
    async def probe_permission(ctx: Context, probe_id: str, target: str, task: str) -> dict:
        """Native approval diagnostic. Inspect target and full task, then choose Allow once or Deny. Execution only records these synthetic inputs locally; no task is sent and no access is granted. Do not choose persistent approval."""
        if not (0 < len(probe_id) <= 80 and 0 < len(target) <= 200 and 0 < len(task) <= 16000):
            raise ValueError("Invalid diagnostic input lengths")
        observation = dict(probe_id=probe_id, target=target, task=task,
                           request_id=ctx.request_id, protocol=ctx.protocol_version,
                           capabilities=ctx.client_capabilities.model_dump(mode="json") if ctx.client_capabilities else None)
        record("tool_executed", **observation)
        return {"state": "probe_executed", "probe_id": probe_id,
                "detail": "Only the synthetic diagnostic was recorded. No task was sent or authorized."}

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    create_probe(args.evidence).run(transport="stdio")
