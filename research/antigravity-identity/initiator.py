"""Terminal stand-in for an initiating desktop, for the Antigravity two-chat test.

Delegates through the test profile's `codex` endpoint over a real stdio MCP session. The
approval form is printed here and only a typed `yes` approves it. Re-running the same
command with the same key resumes the same task instead of creating a new one.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import ElicitResult


async def run(args):
    record = json.loads((args.workspace.expanduser() / ".probe" / "connector-test.json").read_text())
    data = Path(record["data_dir"])
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "local_ai_connector.cli", "mcp", "--config", str(data / "codex.json"), "--start-service"])

    async def approve(ctx, form):
        print("\n=== Approval requested by the initiating side ===\n" + form.message + "\n" + "=" * 49)
        typed = await asyncio.to_thread(input, "Type yes to approve this task (anything else declines): ")
        return ElicitResult(action="accept", content={}) if typed.strip().lower() == "yes" else ElicitResult(action="decline")

    async with Client(params, elicitation_callback=approve) as client:
        if args.command == "status":
            status = json.loads((await client.call_tool("connector_status")).content[0].text)
            print(json.dumps([w for w in status["workers"] if w["id"] == "antigravity"], indent=2))
            return
        result = await client.call_tool("connector_delegate", {
            "target": "antigravity", "message": args.message, "request_key": args.key, "conversation_mode": "existing",
            "timeout_seconds": args.wait, "target_chat": args.chat, "binding_revision": args.revision})
        print(result.content[0].text if result.content else result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("~/Developer/agy-identity-probe"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    task = commands.add_parser("delegate")
    task.add_argument("key")
    task.add_argument("message")
    task.add_argument("--chat", required=True, help="bound chat label shown by status")
    task.add_argument("--revision", type=int, required=True, help="binding revision shown by status")
    task.add_argument("--wait", type=int, default=5, help="seconds to wait for the answer before returning 'running'")
    asyncio.run(run(parser.parse_args()))
