import json
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import ElicitResult


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
@pytest.mark.parametrize("field_type", ["text", "boolean"])
@pytest.mark.parametrize("action", ["accept", "cancel"])
@pytest.mark.parametrize("server", ["sdk", "wire", "wire_forward"])
async def test_probe_round_trip_records_no_input_values(tmp_path, mode, field_type, action, server):
    evidence = tmp_path / "evidence.jsonl"
    requests = []

    async def callback(ctx, params):
        requests.append(params)
        if server == "wire_forward":
            assert params.meta is not None and params.meta.get("progress_token") is not None
        assert params.requested_schema == {
            "type": "object", "properties": {"value": {"type": "string" if field_type == "text" else "boolean"}}, "required": ["value"],
        }
        return ElicitResult(action=action, content={"value": "DO_NOT_LOG_THIS_INPUT" if field_type == "text" else True} if action == "accept" else None)

    args = [str(Path(__file__).with_name("probe.py" if server == "sdk" else "wire_probe.py")), "--evidence", str(evidence)]
    if server.startswith("wire"):
        args.extend(["--mode", "legacy" if mode == "legacy" else "modern"])
    if server == "wire_forward":
        args.append("--forward-progress-token")
    params = StdioServerParameters(command=sys.executable, args=args)
    async with Client(params, mode=mode, elicitation_callback=callback) as client:
        tools = (await client.list_tools()).tools
        assert [t.name for t in tools] == ["probe_form"]
        async def progress(*args):
            pass

        result = await client.call_tool("probe_form", {"field_type": field_type, "probe_id": "synthetic-probe"}, progress_callback=progress)
        assert not result.is_error, result
        observed = json.loads(result.content[0].text)
        assert observed["action"] == action
        assert observed["state"] == "diagnostic_complete"
        assert len(requests) == 1
    content = evidence.read_text()
    rows = [json.loads(line) for line in content.splitlines() if json.loads(line)["event"].startswith("form_")]
    assert [row["event"] for row in rows] == ["form_requested", "form_returned"]
    assert rows[1]["action"] == action
    assert "DO_NOT_LOG_THIS_INPUT" not in content and "requestState" not in content
    assert evidence.stat().st_mode & 0o777 == 0o600
