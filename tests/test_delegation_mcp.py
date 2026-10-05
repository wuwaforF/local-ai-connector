import asyncio
import json
import socket
import sys

import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import ElicitResult

from local_ai_connector.server import create_app


@pytest.fixture
async def running(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    (tmp_path / "server.json").write_text(json.dumps({"admin_token": "admin", "peers": {"a": "peer-a", "b": "peer-b"}, "approval_tokens": {"a": "approval-a"}}))
    paths = []
    for peer in ("a", "b"):
        config = {"url": url, "peer": peer, "token": f"peer-{peer}", "client": "generic"}
        if peer == "a": config.update(tool_profile="requester", approval_token="approval-a")
        path = tmp_path / f"{peer}.json"
        path.write_text(json.dumps(config))
        paths.append(path)
    app = create_app(tmp_path)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done(): await task
                await asyncio.sleep(0.01)
        yield app, url, paths
    finally:
        server.should_exit = True
        await task


def parameters(path):
    return StdioServerParameters(command=sys.executable, args=["-m", "local_ai_connector.cli", "mcp", "--config", str(path)])


@pytest.mark.parametrize("decision", ["accept", "decline", "cancel"])
async def test_real_stdio_confirmation_and_single_call_result(running, decision):
    app, url, paths = running
    prompts = []
    async def confirmation(ctx, params):
        prompts.append(params.message)
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        return ElicitResult(action=decision, content={} if decision == "accept" else None)
    async with Client(parameters(paths[0]), mode="legacy", elicitation_callback=confirmation) as requester, Client(parameters(paths[1]), mode="legacy") as worker:
        names = {t.name for t in (await requester.list_tools()).tools}
        assert names == {"connector_status", "connector_delegate", "connector_continue", "connector_archive", "connector_workflow_start", "connector_workflow_status", "connector_workflow_resume", "connector_workflow_cancel"}
        assert len((await worker.list_tools()).tools) == 10
        assert "approval-a" not in json.dumps((await requester.list_tools()).model_dump(), default=str)
        async def answer():
            r = await worker.call_tool("connector_receive", {"timeout": 5})
            q = json.loads(r.content[0].text)["messages"][0]
            return await worker.call_tool("connector_send", {"channel": q["channel"], "message": "genuine test worker result", "reply_to": q["id"], "message_key": "answer"})
        worker_task = asyncio.create_task(answer()) if decision == "accept" else None
        args = {"conversation_mode": "existing", "target": "b", "message": "Original task", "request_key": "one", "timeout_seconds": 3}
        result = await requester.call_tool("connector_delegate", args)
        assert not result.is_error, result.content
        parsed = json.loads(result.content[0].text)
        assert len(prompts) == 1 and "Original task" in prompts[0]
        if worker_task:
            assert not (await worker_task).is_error
            assert parsed["state"] == "completed" and parsed["answer"]["body"] == "genuine test worker result"
            again = await requester.call_tool("connector_delegate", args)
            assert not again.is_error and len(prompts) == 1
            assert json.loads(again.content[0].text) == parsed
        else:
            assert parsed["state"] == "denied"
        audit = app.state.broker.db.execute("SELECT decision FROM approval_decisions").fetchall()
        assert [r["decision"] for r in audit] == [decision]


async def test_client_without_elicitation_fails_before_opening_channel(running):
    app, _, paths = running
    async with Client(parameters(paths[0]), mode="legacy") as requester:
        r = await requester.call_tool("connector_delegate", {"conversation_mode": "existing", "target": "b", "message": "task", "request_key": "missing-capability"})
        assert r.is_error and "host_confirmation_unavailable" in r.content[0].text
        assert app.state.broker.snapshot()["channels"] == []
