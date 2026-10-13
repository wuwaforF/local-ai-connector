"""Terminal stand-in for an initiating desktop in the Codex direct wake-up test.

Starts tasks for the test profile's `codex` worker through its `antigravity` endpoint over a real
stdio MCP session. The approval form is printed here and only a typed `yes` approves it. Re-running
`delegate` with the same key resumes the same task instead of creating a new one. `status` also
shows recent wake dispatches and incidents, read directly from the profile's databases.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import ElicitResult


def _rows(path: Path, query: str) -> list[dict]:
    if not path.exists():
        return []
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(query)]
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def _time(value) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%H:%M:%S UTC")


def show_wake(data: Path):
    print("\nRecent wake dispatches:")
    for d in _rows(data / "wakeup.sqlite3", "SELECT * FROM dispatches ORDER BY created DESC LIMIT 5"):
        events = _rows(data / "wakeup.sqlite3",
                       f"SELECT stage FROM dispatch_events WHERE dispatch_id='{d['id']}' ORDER BY seq")
        print(f"  {_time(d['created'])}  {d['state']:<14} target={d['target']}  "
              f"stages={' > '.join(e['stage'] for e in events)}")
    print("Recent incidents:")
    for i in _rows(data / "state.sqlite3", "SELECT * FROM incidents ORDER BY created DESC LIMIT 8"):
        print(f"  {_time(i['created'])}  {i['peer']:<12} {i['code']}: {i['detail']}")


async def run(args):
    record = json.loads((args.workspace.expanduser() / ".probe" / "connector-test.json").read_text())
    data = Path(record["data_dir"])
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "local_ai_connector.cli", "mcp", "--config", str(data / "antigravity.json"), "--start-service"])

    async def approve(ctx, form):
        print("\n=== Approval requested by the initiating side ===\n" + form.message + "\n" + "=" * 49)
        typed = await asyncio.to_thread(input, "Type yes to approve this task (anything else declines): ")
        return ElicitResult(action="accept", content={}) if typed.strip().lower() == "yes" else ElicitResult(action="decline")

    async with Client(params, elicitation_callback=approve) as client:
        if args.command == "status":
            status = json.loads((await client.call_tool("connector_status")).content[0].text)
            print(json.dumps([w for w in status["workers"] if w["id"] == "codex"], indent=2))
            show_wake(data)
            return
        result = await client.call_tool("connector_delegate", {
            "target": "codex", "message": args.message, "request_key": args.key, "conversation_mode": "existing",
            "timeout_seconds": args.wait, "target_chat": args.chat, "binding_revision": args.revision})
        print(result.content[0].text if result.content else result)
        show_wake(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("~/Developer/codex-wake-test"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    task = commands.add_parser("delegate")
    task.add_argument("key")
    task.add_argument("message")
    task.add_argument("--chat", required=True, help="bound chat label shown by status")
    task.add_argument("--revision", type=int, required=True, help="binding revision shown by status")
    task.add_argument("--wait", type=int, default=120, help="seconds to wait for the answer before returning 'running'")
    asyncio.run(run(parser.parse_args()))
