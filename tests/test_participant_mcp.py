import asyncio
import json
import socket
import sys

import pytest
import uvicorn
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitRequest, ElicitResult, InputRequiredResult, ListRootsResult

from local_ai_connector.mcp_server import create_mcp, serve
from local_ai_connector.server import create_app


@pytest.fixture
async def participants(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    peers = ("writer", "reviewer")
    (tmp_path / "server.json").write_text(json.dumps({
        "admin_token": "admin", "peers": {p: f"peer-{p}" for p in peers},
        "approval_tokens": {p: f"approval-{p}" for p in peers},
    }))
    paths = []
    for peer in peers:
        path = tmp_path / f"{peer}.json"
        path.write_text(json.dumps({"url": url, "peer": peer, "token": f"peer-{peer}",
                                   "client": "generic", "tool_profile": "participant",
                                   "approval_token": f"approval-{peer}", "mcp_locale": "en-US"}))
        paths.append(path)
    app = create_app(tmp_path)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield app, paths
    finally:
        server.should_exit = True
        await task


def parameters(path):
    return StdioServerParameters(command=sys.executable,
        args=["-m", "local_ai_connector.cli", "mcp", "--config", str(path)])


@pytest.mark.parametrize("token", [None, 0, "chat-routing-token"])
@pytest.mark.parametrize("initiator", [0, 1])
@pytest.mark.parametrize("decision", ["accept", "decline", "cancel"])
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"], ids=["legacy", "modern"])
async def test_participant_can_delegate_and_serve_in_both_directions(participants, initiator, decision, mode, token):
    app, paths = participants
    prompts = []

    async def confirmation(ctx, params):
        prompts.append(params)
        expected_meta = {} if token is None else {"progress_token": token}
        assert (params.meta or {}) == expected_meta
        assert params.message.replace("Task 原文", "").isascii()
        assert params.requested_schema == {"type": "object", "properties": {}}
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        return ElicitResult(action=decision, content={} if decision == "accept" else None)

    async with Client(parameters(paths[0]), mode=mode, elicitation_callback=confirmation) as first, Client(
        parameters(paths[1]), mode=mode, elicitation_callback=confirmation
    ) as second:
        clients = (first, second)
        requester, worker = clients[initiator], clients[1 - initiator]
        for client in clients:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == {"connector_status", "connector_delegate", "connector_continue", "connector_archive", "connector_receive", "connector_send", "connector_finish", "connector_workflow_start", "connector_workflow_status", "connector_workflow_resume", "connector_workflow_cancel"}
            assert "approval-" not in json.dumps([t.model_dump() for t in tools], default=str)
            if mode == "legacy":
                assert client.instructions.isascii()
            else:
                assert client.protocol_version == "2026-07-28"

        async def answer():
            r = await worker.call_tool("connector_receive", {"timeout": 5})
            q = json.loads(r.content[0].text)["messages"][0]
            return await worker.call_tool("connector_send", {"channel": q["channel"], "message": "Actual peer answer",
                "reply_to": q["id"], "message_key": "answer"})

        worker_task = asyncio.create_task(answer()) if decision == "accept" else None
        args = {"conversation_mode": "existing", "target": ("writer", "reviewer")[1 - initiator], "message": "Task 原文", "request_key": "one", "timeout_seconds": 3}
        meta = {"private-test-metadata": "must-not-propagate"}
        if token is not None:
            meta["progress_token"] = token
        r = await requester.call_tool("connector_delegate", args, meta=meta)
        assert not r.is_error, r.content
        result = json.loads(r.content[0].text)
        assert len(prompts) == 1 and "Task 原文" in prompts[0].message
        if worker_task:
            assert not (await worker_task).is_error
            assert result["state"] == "completed" and result["answer"]["body"] == "Actual peer answer"
            again = await requester.call_tool("connector_delegate", args)
            assert json.loads(again.content[0].text) == result and len(prompts) == 1
        else:
            assert result["state"] == "denied"
            assert app.state.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        audit = app.state.broker.db.execute("SELECT peer,decision FROM approval_decisions").fetchall()
        assert [tuple(r) for r in audit] == [(("writer", "reviewer")[initiator], decision)]


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"], ids=["legacy", "modern"])
async def test_participant_without_host_confirmation_never_opens_channel(participants, mode):
    app, paths = participants
    async with Client(parameters(paths[0]), mode=mode) as client:
        result = await client.call_tool("connector_delegate", {"conversation_mode": "existing", "target": "reviewer", "message": "Task", "request_key": "unavailable"})
        assert result.is_error and "host_confirmation_unavailable" in result.content[0].text
        assert app.state.broker.snapshot()["channels"] == []


@pytest.mark.parametrize("mutation", ["tampered_state", "changed_message", "changed_key", "missing_state"])
async def test_modern_approval_round_is_bound_to_original_task(participants, mutation):
    app, paths = participants

    async def confirmation(ctx, params):
        raise AssertionError("The raw session API must leave the input-required round to this test")

    async with Client(parameters(paths[0]), mode="2026-07-28", elicitation_callback=confirmation) as client:
        args = {"conversation_mode": "existing", "target": "reviewer", "message": "Review the test draft", "request_key": "bound-task", "timeout_seconds": 1}
        first = await client.session.call_tool("connector_delegate", args, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        assert first.result_type == "input_required"
        assert first.input_requests and len(first.input_requests) == 1
        key, request = next(iter(first.input_requests.items()))
        assert isinstance(request, ElicitRequest) and request.method == "elicitation/create"
        assert request.params.message.isascii() and args["message"] in request.params.message
        assert request.params.requested_schema == {"type": "object", "properties": {}}
        channels = app.state.broker.snapshot()["channels"]
        assert len(channels) == 1 and channels[0]["status"] == "pending"
        assert first.request_state and first.request_state != channels[0]["id"]
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0

        changed_args = dict(args)
        state = first.request_state
        if mutation == "tampered_state":
            state += "tampered"
        elif mutation == "changed_message":
            changed_args["message"] = "Perform a different task"
        elif mutation == "changed_key":
            changed_args["request_key"] = "other-task"
        else:
            state = None
        responses = {key: ElicitResult(action="accept", content={})}
        if mutation == "missing_state":
            rejected = await client.session.call_tool("connector_delegate", changed_args,
                input_responses=responses, request_state=state, allow_input_required=True)
            assert rejected.is_error and "invalid_confirmation" in rejected.content[0].text
        else:
            with pytest.raises(MCPError, match="Invalid or expired requestState"):
                await client.session.call_tool("connector_delegate", changed_args,
                    input_responses=responses, request_state=state, allow_input_required=True)
        assert app.state.broker.snapshot()["channels"] == channels
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0] == 0
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0

        declined = await client.session.call_tool("connector_delegate", args,
            input_responses={key: ElicitResult(action="decline")}, request_state=first.request_state,
            allow_input_required=True)
        assert not declined.is_error and json.loads(declined.content[0].text)["state"] == "denied"
        again = await client.call_tool("connector_delegate", args)
        assert not again.is_error and json.loads(again.content[0].text)["state"] == "denied"
        audit = app.state.broker.db.execute("SELECT peer,decision FROM approval_decisions").fetchall()
        assert [tuple(row) for row in audit] == [("writer", "decline")]
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("scenario", ["missing_response", "unexpected_content", "wrong_type", "expired", "revoked", "declined"])
async def test_modern_confirmation_response_and_authority_are_rechecked(participants, scenario):
    app, paths = participants

    async def confirmation(ctx, params):
        raise AssertionError("Only the raw protocol test supplies confirmation")

    async with Client(parameters(paths[0]), mode="2026-07-28", elicitation_callback=confirmation) as client:
        args = {"conversation_mode": "existing", "target": "reviewer", "message": "Test task", "request_key": "response-check", "timeout_seconds": 1}
        first = await client.session.call_tool("connector_delegate", args, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        key = next(iter(first.input_requests))
        channel = app.state.broker.snapshot()["channels"][0]
        responses = {key: ElicitResult(action="accept", content={})}
        if scenario == "missing_response":
            responses = {}
        elif scenario == "unexpected_content":
            responses[key] = ElicitResult(action="accept", content={"confirmed": "true"})
        elif scenario == "wrong_type":
            responses[key] = ListRootsResult(roots=[])
        elif scenario == "declined":
            responses[key] = ElicitResult(action="decline")
        elif scenario == "revoked":
            await app.state.broker.revoke(channel["id"])
        else:
            app.state.broker.clock = lambda: channel["expires"] + 1
        result = await client.session.call_tool("connector_delegate", args, input_responses=responses,
            request_state=first.request_state, allow_input_required=True)
        if scenario == "missing_response":
            assert isinstance(result, InputRequiredResult) and key in result.input_requests
        elif scenario in ("unexpected_content", "wrong_type"):
            assert result.is_error and "invalid_confirmation" in result.content[0].text
        else:
            expected = "denied" if scenario == "declined" else scenario
            assert not result.is_error and json.loads(result.content[0].text)["state"] == expected
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        audit = app.state.broker.db.execute("SELECT decision FROM approval_decisions").fetchall()
        assert [row[0] for row in audit] == (["decline"] if scenario == "declined" else [])


async def test_modern_form_restart_rejects_old_state_and_recovers_same_task(participants):
    app, paths = participants

    async def confirmation(ctx, params):
        raise AssertionError("Only the raw protocol test supplies confirmation")

    args = {"conversation_mode": "existing", "target": "reviewer", "message": "Test task", "request_key": "restart-form", "timeout_seconds": 1}
    async with Client(parameters(paths[0]), mode="2026-07-28", elicitation_callback=confirmation) as client:
        first = await client.session.call_tool("connector_delegate", args, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
    async with Client(parameters(paths[0]), mode="2026-07-28", elicitation_callback=confirmation) as client:
        key = next(iter(first.input_requests))
        with pytest.raises(MCPError, match="Invalid or expired requestState"):
            await client.session.call_tool("connector_delegate", args,
                input_responses={key: ElicitResult(action="accept", content={})},
                request_state=first.request_state, allow_input_required=True)
        fresh = await client.session.call_tool("connector_delegate", args, allow_input_required=True)
        assert isinstance(fresh, InputRequiredResult) and fresh.request_state != first.request_state
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1
        result = await client.session.call_tool("connector_delegate", args,
            input_responses={key: ElicitResult(action="cancel")}, request_state=fresh.request_state,
            allow_input_required=True)
        assert json.loads(result.content[0].text)["state"] == "denied"
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"], ids=["legacy", "modern"])
async def test_participants_continue_clarification_without_another_approval(participants, mode):
    app, paths = participants
    approvals = []

    async def confirmation(ctx, params):
        approvals.append(params.message)
        return ElicitResult(action="accept", content={})

    async with Client(parameters(paths[0]), mode=mode, elicitation_callback=confirmation) as requester, Client(
        parameters(paths[1]), mode=mode, elicitation_callback=confirmation
    ) as worker:
        args = {"conversation_mode": "existing", "target": "reviewer", "message": "Greet the user by name", "request_key": "greeting", "timeout_seconds": 3}
        task = asyncio.create_task(requester.call_tool("connector_delegate", args))
        received = await worker.call_tool("connector_receive", {"timeout": 5})
        initial = json.loads(received.content[0].text)["messages"][0]
        question = await worker.call_tool("connector_send", {"channel": initial["channel"], "kind": "question",
            "message": "What name should I use?", "reply_to": initial["id"], "message_key": "ask-name"})
        followup = json.loads(question.content[0].text)
        result = json.loads((await task).content[0].text)
        assert result["state"] == "input_required" and result["questions"][0]["id"] == followup["id"]
        resume = asyncio.create_task(requester.call_tool("connector_delegate", {**args, "reply_to": followup["id"], "reply": "Taylor"}))
        received = await worker.call_tool("connector_receive", {"channel": initial["channel"], "after": initial["seq"], "timeout": 5})
        received = json.loads(received.content[0].text)
        supplement = received["messages"][0]
        assert supplement["body"] == "Taylor" and supplement["reply_to"] == followup["id"]
        await worker.call_tool("connector_send", {"channel": initial["channel"], "message": "Hello, Taylor.",
            "reply_to": initial["id"], "message_key": "greeting-answer"})
        result = json.loads((await resume).content[0].text)
        assert result["state"] == "completed" and result["answer"]["body"] == "Hello, Taylor."

        next_round = asyncio.create_task(requester.call_tool("connector_continue", {
            "channel": initial["channel"], "message": "Add a short summary.", "request_key": "summary",
            "timeout_seconds": 3}))
        received = await worker.call_tool("connector_receive", {"channel": initial["channel"],
            "after": received["cursor"], "timeout": 5})
        continuation = json.loads(received.content[0].text)["messages"][0]
        assert continuation["channel"] == initial["channel"] and continuation["kind"] == "question"
        await worker.call_tool("connector_send", {"channel": initial["channel"], "message": "Summary only.",
            "reply_to": continuation["id"], "message_key": "summary-answer"})
        continued = json.loads((await next_round).content[0].text)
        assert continued["state"] == "completed" and continued["channel"] == initial["channel"]
        assert continued["answer"]["body"] == "Summary only."
        assert continued["question_id"] == continuation["id"]
        assert len(approvals) == 1
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1
        assert app.state.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 6


@pytest.mark.parametrize("token", [None, ""])
def test_participant_requires_private_approval_credential(tmp_path, token):
    path = tmp_path / "peer.json"
    path.write_text(json.dumps({"url": "http://127.0.0.1:1", "peer": "writer", "token": "peer",
                               "tool_profile": "participant", "approval_token": token}))
    with pytest.raises(ValueError, match="participant profile requires a private approval_token"):
        serve(path)


def test_combined_tools_require_decision_callback():
    with pytest.raises(ValueError, match="decision callback"):
        create_mcp(None, combined=True)

@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_configured_tool_permission_starts_once_and_continues_on_channel(participants, mode):
    app, paths = participants
    config = json.loads(paths[0].read_text())
    config["approval_transport"] = "host_tool_permission"
    paths[0].write_text(json.dumps(config))
    # This client simulates the configured host gate; it proves no human consent.
    async with Client(parameters(paths[0]), mode=mode) as sender, Client(parameters(paths[1]), mode=mode) as worker:
        tools = {t.name: t for t in (await sender.list_tools()).tools}
        assert tools["connector_delegate"].meta["anthropic/requiresUserInteraction"] is True
        assert tools["connector_archive"].meta["anthropic/requiresUserInteraction"] is True
        assert all(not (t.meta or {}).get("anthropic/requiresUserInteraction")
                   for n, t in tools.items() if n not in ("connector_delegate", "connector_archive"))
        args = {"conversation_mode": "existing", "target": "reviewer", "message": "Original complete task", "request_key": "host-gated"}
        result = await sender.call_tool("connector_delegate", args)
        assert not result.is_error, result.content
        task = json.loads(result.content[0].text)
        assert task["state"] == "active" and task["next_tool"] == "connector_receive"
        channel = task["channel"]
        again = await sender.call_tool("connector_delegate", args)
        assert not again.is_error
        changed = await sender.call_tool("connector_delegate", {**args, "message": "Substituted task"})
        assert changed.is_error
        questions = json.loads((await worker.call_tool("connector_receive", {"channel": channel, "timeout": 0})).content[0].text)
        q = questions["messages"][0]
        assert q["body"] == args["message"]
        reply = await worker.call_tool("connector_send", {"channel": channel, "message": "Real synthetic result", "message_key": "reply", "reply_to": q["id"]})
        assert not reply.is_error
        received = json.loads((await sender.call_tool("connector_receive", {"channel": channel, "timeout": 0})).content[0].text)
        assert received["messages"][0]["body"] == "Real synthetic result"
        assert not (await sender.call_tool("connector_finish", {"channel": channel})).is_error
        audit = app.state.broker.db.execute("SELECT source, decision FROM approval_decisions").fetchall()
        assert [dict(r) for r in audit] == [{"source": "host_tool_permission", "decision": "accept"}]
        assert app.state.broker.db.execute("SELECT count(*) FROM messages WHERE kind='question'").fetchone()[0] == 1


@pytest.mark.parametrize("transport,combined", [("unknown", True), (None, True), ("host_tool_permission", False)])
def test_tool_permission_configuration_fails_closed(transport, combined):
    with pytest.raises(ValueError):
        create_mcp(None, decision=object(), combined=combined, approval_transport=transport)
