"""Antigravity lifecycle hook for the identity diagnostic.

Records the host-supplied payload (conversationId, toolCall, stepIdx) for each MCP tool call.
For identity_probe calls it also answers PreToolUse with an `overwrite` that sets
`attestation` to a fresh nonce, to learn whether a hook-injected value reaches the MCP server
even when the model supplied its own. The decision is always "ask", so the host's normal
permission handling (including any "Always allow" choice) is unchanged.
"""
import json
import os
import secrets
import sys
import time

SECRETISH = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CSRF", "COOKIE", "AUTH")
PROBE = "identity_probe"


def host_env():
    return {k: ("<present>" if any(w in k for w in SECRETISH) else v)
            for k, v in os.environ.items() if k.startswith(("ANTIGRAVITY_", "GEMINI_", "AGY_"))}


def injection(tool_call: dict, nonce: str):
    """Build an overwrite that adds attestation to the MCP arguments, whatever shape the host uses."""
    args = tool_call.get("args")
    if isinstance(args, dict) and "note" in args:
        return {"attestation": nonce}, "top-level"
    if isinstance(args, dict):
        for key, value in args.items():
            if isinstance(value, dict) and "note" in value:
                return {key: {**value, "attestation": nonce}}, f"nested:{key}"
            if isinstance(value, str) and '"note"' in value:
                try:
                    inner = json.loads(value)
                except ValueError:
                    continue
                if isinstance(inner, dict):
                    return {key: json.dumps({**inner, "attestation": nonce})}, f"json-string:{key}"
    return None, "not-found"


def main():
    event, log = sys.argv[1], sys.argv[2]
    # --deny-server NAME: block a server in this workspace (protects a live installation during tests).
    denied = sys.argv[sys.argv.index("--deny-server") + 1] if "--deny-server" in sys.argv else None
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {"unparsed": raw[:2000]}
    entry = {"time": time.time(), "event": event, "pid": os.getpid(), "env": host_env(), "payload": payload}
    output = {}
    if event == "pre":
        output = {"decision": "ask"}
        tool_call = payload.get("toolCall") if isinstance(payload, dict) else None
        server = (tool_call.get("args") or {}).get("ServerName") if isinstance(tool_call, dict) else None
        if denied is not None and server == denied:
            output = {"decision": "deny", "reason": f"{denied} is blocked in this test workspace"}
        elif isinstance(tool_call, dict) and PROBE in json.dumps(tool_call):
            nonce = "hook-" + secrets.token_hex(6)
            overwrite, shape = injection(tool_call, nonce)
            entry.update(nonce=nonce, overwrite_shape=shape)
            if overwrite is not None:
                output["overwrite"] = overwrite
    entry["output"] = output
    with open(log, "a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    sys.stdout.write(json.dumps(output))


if __name__ == "__main__":
    main()
