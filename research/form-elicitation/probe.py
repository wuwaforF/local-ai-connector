"""Isolated form diagnostic. No connector service or task credentials are used."""
import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.request_state import RequestStateSecurity
from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult, ToolAnnotations
from mcp_types.version import MODERN_PROTOCOL_VERSIONS


def create_probe(evidence: Path):
    server = MCPServer(
        "Connector Form Diagnostic",
        instructions="Use probe_form only for the requested diagnostic. It does not send tasks or authorize any action. Let the user fill any actual form; report the returned action accurately.",
        request_state_security=RequestStateSecurity.ephemeral(ttl=300),
    )

    def record(event):
        # Keep input values and authenticated requestState out of the diagnostic log.
        data = (json.dumps({"time": time.time(), **event}, ensure_ascii=False) + "\n").encode()
        fd = os.open(evidence, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
    async def probe_form(ctx: Context, field_type: Literal["text", "boolean"], probe_id: str) -> dict | InputRequiredResult:
        """Test one native MCP form. Choose text or boolean; probe_id is a short diagnostic label. No task is sent and no permission is granted by the form. The user may enter DIAGNOSTIC for text or select Yes for boolean. Report the actual returned action, including cancel, then stop."""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", probe_id):
            raise ValueError("probe_id must be a short alphanumeric diagnostic label")
        schema = {"type": "object", "properties": {"value": {"type": "string" if field_type == "text" else "boolean"}}, "required": ["value"]}
        modern = ctx.protocol_version in MODERN_PROTOCOL_VERSIONS
        caps = ctx.client_capabilities
        elicitation = caps.elicitation if caps else None
        observation = {
            "probe_id": probe_id, "field_type": field_type,
            "protocol": ctx.protocol_version, "modern": modern,
            "can_send_request": ctx.session.can_send_request,
            "elicitation_present": elicitation is not None,
            "form_present": elicitation is not None and elicitation.form is not None,
        }
        if elicitation is None or (elicitation.form is None and elicitation.url is not None):
            record({**observation, "event": "capability_missing"})
            return {**observation, "state": "capability_missing"}
        message = (
            f"MCP form diagnostic: {field_type}. This form does not send a task or grant permission. "
            + ("Enter DIAGNOSTIC and submit." if field_type == "text" else "Select Yes and submit.")
        )
        if modern:
            responses = ctx.input_responses or {}
            if "probe" not in responses:
                started = time.time()
                record({**observation, "event": "form_requested", "schema": schema})
                return InputRequiredResult(
                    input_requests={"probe": ElicitRequest(params=ElicitRequestFormParams(message=message, requested_schema=schema))},
                    request_state=json.dumps({"probe_id": probe_id, "field_type": field_type, "started": started}),
                )
            state = json.loads(ctx.request_state) if isinstance(ctx.request_state, str) else None
            if not isinstance(state, dict) or state.get("probe_id") != probe_id or state.get("field_type") != field_type:
                raise ValueError("Diagnostic response state mismatch")
            started = state["started"]
            confirmation = responses["probe"]
            if not isinstance(confirmation, ElicitResult):
                raise ValueError("Expected an elicitation response")
        else:
            started = time.time()
            record({**observation, "event": "form_requested", "schema": schema})
            async with asyncio.timeout(300):
                confirmation = await ctx.session.elicit_form(
                    message=message, requested_schema=schema, related_request_id=ctx.request_id,
                )
        value = confirmation.content.get("value") if isinstance(confirmation.content, dict) else None
        result = {
            **observation, "state": "diagnostic_complete", "action": confirmation.action,
            "response_value_type": type(value).__name__, "host_meta_present": confirmation.meta is not None,
            "elapsed_ms": round((time.time() - started) * 1000, 3),
        }
        record({**result, "event": "form_returned"})
        return result

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    create_probe(args.evidence).run(transport="stdio")
