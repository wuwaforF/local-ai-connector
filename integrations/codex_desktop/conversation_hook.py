"""Owner-installed hook for the broker's exact, single-use native chat plans."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from local_ai_connector.conversations import handle_hook
from local_ai_connector.core import ConnectorError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--peer", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    args = parser.parse_args()
    event = None
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("hook input exceeds size limit")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input is not an object")
        if event.get("session_id") != args.session or event.get("cwd") != args.cwd:
            print("{}")
            return 0
        with sqlite3.connect(args.store.resolve().as_uri() + "?mode=rw", uri=True,
                             timeout=5, isolation_level=None) as db:
            db.row_factory = sqlite3.Row
            def verify_selected(selected, ingress):
                from local_ai_connector.codex_binding import CodexCatalog
                config = json.loads((args.store.parent / 'server.json').read_bytes())
                source = CodexCatalog(config.get('codex_binding_cli'))
                asyncio.run(source.verify(selected, ingress, exact=False))
            result = handle_hook(db, event, args.peer, args.session, args.cwd,
                                 selected_verifier=verify_selected)
    except (ConnectorError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f"Native conversation hook failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if isinstance(event, dict) and event.get("hook_event_name") == "PermissionRequest":
            result = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
                "behavior": "deny", "message": "No active, unused connector authorization matches this native action."}}}
        else:
            return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
