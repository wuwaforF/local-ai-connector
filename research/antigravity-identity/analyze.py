"""Correlate hook records with MCP-received calls by nonce and note; print one row per call."""
import argparse
import json
from pathlib import Path


def load(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def analyze(logs: Path) -> dict:
    hooks, calls = load(logs / "hooks.jsonl"), [c for c in load(logs / "mcp.jsonl") if c.get("event") == "call"]
    starts = [c for c in load(logs / "mcp.jsonl") if c.get("event") == "start"]
    pre = [h for h in hooks if h["event"] == "pre" and h.get("nonce")]
    rows = []
    for call in calls:
        match = next((h for h in pre if h["nonce"] == call["attestation"]), None)
        # Correlation that needs no overwrite: pre-tool hooks whose arguments carried this call's note.
        by_note = sorted({h.get("payload", {}).get("conversationId") for h in hooks if h["event"] == "pre"
                          and json.dumps(call["note"]) in json.dumps(h.get("payload", {}).get("toolCall"))} - {None})
        rows.append({"note": call["note"], "mcp_pid": call["pid"], "received_attestation": call["attestation"],
                     "conversations_seen_by_hooks_for_note": by_note,
                     "hook_conversation": (match or {}).get("payload", {}).get("conversationId"),
                     "hook_env_conversation": (match or {}).get("env", {}).get("ANTIGRAVITY_CONVERSATION_ID"),
                     "overwrite_shape": (match or {}).get("overwrite_shape"), "meta_keys": sorted((call.get("meta") or {}).keys())
                     if isinstance(call.get("meta"), dict) else call.get("meta")})
    return {"mcp_processes": sorted({s["pid"] for s in starts}), "hook_events": len(hooks),
            "pre_tool_hooks_for_probe": len(pre), "calls": rows,
            "invocation_conversations": sorted({h.get("payload", {}).get("conversationId") for h in hooks
                                                if h["event"] == "invocation"} - {None})}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", type=Path)
    print(json.dumps(analyze(parser.parse_args().logs), indent=2))
