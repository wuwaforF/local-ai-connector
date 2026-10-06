"""Isolated identity diagnostic: what an Antigravity chat's MCP tool call actually carries.

No connector code, credentials or tasks. The single tool records the arguments it received,
the request _meta, the protocol version and the serving process, so separate chats can be
compared. Pair it with hook.py, which records the host's conversationId for the same call.
"""
import argparse
import json
import os
import time
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations

SECRETISH = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CSRF", "COOKIE", "AUTH")


def redacted_env():
    """Host-related variables; values that look secret are reduced to their presence."""
    out = {}
    for key, value in os.environ.items():
        if key.startswith(("ANTIGRAVITY_", "GEMINI_", "AGY_")):
            out[key] = "<present>" if any(word in key for word in SECRETISH) else value
    return out


def create_probe(log: Path):
    server = MCPServer("Identity Probe", instructions=(
        "Diagnostic only. Call identity_probe when the user asks, with the exact note they give. "
        "Leave attestation empty unless the user explicitly provides a value. Report the tool result verbatim."))

    def record(event):
        line = (json.dumps({"time": time.time(), **event}, ensure_ascii=False, default=str) + "\n").encode()
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
    async def identity_probe(ctx: Context, note: str, attestation: str = "") -> dict:
        """Diagnostic: report what this call carried. note is the label the user gives (for example A1).
        attestation stays empty unless the user explicitly gives a value. Sends nothing anywhere."""
        meta = ctx.request_context.meta
        received = {"note": note, "attestation": attestation, "pid": os.getpid(), "ppid": os.getppid(),
                    "protocol": getattr(ctx, "protocol_version", None),
                    "meta": meta if isinstance(meta, dict) else (meta.model_dump() if hasattr(meta, "model_dump") else str(meta)),
                    "env": redacted_env()}
        record({"event": "call", **received})
        return received

    record({"event": "start", "pid": os.getpid(), "ppid": os.getppid(), "env": redacted_env()})
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    create_probe(args.log.resolve()).run(transport="stdio")
