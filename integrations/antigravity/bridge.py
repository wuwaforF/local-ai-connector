"""CommandAdapter bridge using agentapi in Antigravity's official sidecar environment."""
import json
import subprocess
import sys
from uuid import UUID


def confirmed_target(reply, target):
    metadata = reply["response"]["conversationMetadata"]["metadata"]
    actual = {"conversation_id": metadata["rootConversationId"],
              "project_id": metadata["projectId"],
              "workspace_uri": target["workspace_uri"]}
    if actual != target or target["workspace_uri"] not in metadata["workspaceUris"]:
        raise ValueError("Host metadata does not match the pinned root conversation and workspace")
    return actual


def handle(request, run=subprocess.run):
    op = request["op"]
    target = request["target"]
    if set(target) != {"conversation_id", "project_id", "workspace_uri"}:
        raise ValueError("Explicit conversation, project and workspace are required")
    conversation = str(UUID(target["conversation_id"]))
    UUID(target["project_id"])
    if op == "status":
        # Metadata confirms identity but supplies no activity state in Antigravity 2.15.0.
        return {"ok": True, "state": "unknown"}
    if op == "reconcile":
        # agentapi does not expose a dispatch lookup; never infer non-delivery from silence.
        return {"ok": True, "result": "unknown"}
    if op == "confirm":
        argv = ["agentapi", "get-conversation-metadata", conversation]
    elif op == "send":
        dispatch = str(UUID(request["dispatch_id"]))
        text = request["text"]
        if not isinstance(text, str) or not 0 < len(text) <= 1000 or dispatch not in text:
            raise ValueError("Expected bounded wake instruction with a dispatch identifier")
        argv = ["agentapi", "send-message", conversation, text]
    else:
        raise ValueError("Unsupported bridge operation")
    uncertain = op == "send"
    try:
        result = run(argv, capture_output=True, timeout=25)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "error": "ambiguous" if uncertain else "unavailable",
                "detail": type(exc).__name__}
    if result.returncode != 0:
        return {"ok": False, "error": "ambiguous" if uncertain else "unavailable",
                "detail": "agentapi returned a nonzero exit status"}
    try:
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict) or "error" in reply or not isinstance(reply.get("response"), dict):
            raise ValueError("agentapi response was not successful")
        if op == "confirm":
            return {"ok": True, "target": confirmed_target(reply, target)}
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "error": "ambiguous" if uncertain else "stale_target",
                "detail": "agentapi response could not confirm the requested operation"}
    return {"ok": True, "accepted": True, "host_ref": dispatch}


if __name__ == "__main__":
    try:
        response = handle(json.load(sys.stdin))
    except (ValueError, KeyError, TypeError) as exc:
        response = {"ok": False, "error": "unavailable", "detail": type(exc).__name__}
    print(json.dumps(response))
