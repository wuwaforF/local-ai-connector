import asyncio
import json

import pytest
from mcp import Client
from mcp_types import ElicitResult

from test_participant_mcp import participants, parameters
from test_conversations import REGISTRATION, SESSION, CHILD, event, receipt
from local_ai_connector.conversations import handle_hook


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_native_creation_and_continuation_through_real_mcp(participants, mode):
    app, paths = participants
    broker = app.state.broker
    broker.conversations.registrations["reviewer"] = REGISTRATION
    prompts = []

    async def confirmation(ctx, params):
        prompts.append(params.message)
        assert "new native chat" in params.message
        assert broker.db.execute("SELECT count(*) FROM native_operations").fetchone()[0] == 0
        return ElicitResult(action="accept", content={})

    async with Client(parameters(paths[0]), mode=mode, elicitation_callback=confirmation) as requester, \
            Client(parameters(paths[1]), mode=mode) as worker:
        args = {"target": "reviewer", "message": "Open a new chat and calculate 48 + 37", "request_key": "native-one",
                "conversation_mode": "new", "timeout_seconds": 3}

        async def answer(text):
            result = await worker.call_tool("connector_receive", {"timeout": 3})
            messages = json.loads(result.content[0].text)["messages"]
            question = next(q for q in messages if not q["resolved"] and q["kind"] == "question")
            plan = question["native_execution"]
            handle_hook(broker.db, event(plan), "reviewer", SESSION, "/worker", broker.clock())
            handle_hook(broker.db, receipt(plan), "reviewer", SESSION, "/worker", broker.clock())
            result = await worker.call_tool("connector_send", {"channel": question["channel"], "message": text,
                                            "reply_to": question["id"], "message_key": question["id"]})
            assert not result.is_error, result.content
            return plan

        task = asyncio.create_task(answer("85"))
        result = await requester.call_tool("connector_delegate", args)
        assert not result.is_error, result.content
        first = json.loads(result.content[0].text)
        assert first["conversation"]["thread_id"] == CHILD
        assert (await task)["arguments"]["target"]["type"] == "projectless"
        assert first["next_round"] == {"tool": "connector_continue", "channel": first["channel"]}
        task = asyncio.create_task(answer("100"))
        result = await requester.call_tool("connector_continue", {"channel": first["channel"], "message": "Add 15",
                                            "request_key": "next-round", "timeout_seconds": 3})
        assert not result.is_error, result.content
        second = json.loads(result.content[0].text)
        assert second["conversation"]["thread_id"] == CHILD
        assert second["channel"] == first["channel"] and second["answer"]["body"] == "100"
        assert (await task)["arguments"] == {"threadId": CHILD, "prompt": "Add 15"}
        assert len(prompts) == 1
        assert broker.db.execute("SELECT count(*) FROM approval_decisions").fetchone()[0] == 1


async def test_missing_mode_or_unsupported_new_chat_fails_before_confirmation(participants):
    app, paths = participants
    async def confirmation(ctx, params):
        raise AssertionError("Unfulfillable native creation must fail before confirmation")
    async with Client(parameters(paths[0]), mode="legacy", elicitation_callback=confirmation) as requester:
        args = {"target": "reviewer", "message": "Open a new chat", "request_key": "new"}
        missing = await requester.call_tool("connector_delegate", args)
        assert missing.is_error and "conversation_mode" in missing.content[0].text
        unsupported = await requester.call_tool("connector_delegate", {**args, "conversation_mode": "new"})
        assert unsupported.is_error and "new_conversation_unsupported" in unsupported.content[0].text
        assert app.state.broker.db.execute("SELECT count(*) FROM channels").fetchone()[0] == 0
