from types import SimpleNamespace

import pytest

from local_ai_connector.core import Broker, ConnectorError
from local_ai_connector.delegation import delegate
from test_delegation import Context, operations, worker_reply


@pytest.fixture
def broker(tmp_path):
    broker = Broker(tmp_path / "state.sqlite3")
    broker.register("requester", "requester-token")
    broker.register("worker", "worker-token")
    yield broker
    broker.close()


@pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
async def test_confirmation_and_progress_follow_locale_without_translating_content(broker, locale):
    task = "保留任务原文。 Keep this task exactly."
    answer = "保留回复原文。 Keep this answer exactly."
    ctx = Context()
    schemas = []
    original_elicit = ctx.session.elicit_form

    async def inspect_confirmation(**params):
        schemas.append(params["requested_schema"])
        assert params["related_request_id"] == ctx.request_id
        assert not broker.db.execute("SELECT * FROM messages").fetchall()
        return await original_elicit(**params)

    ctx.session.elicit_form = inspect_confirmation
    call, decide = operations(broker)
    receives = 0

    async def reply_after_wait(ctx, **payload):
        nonlocal receives
        if payload["action"] == "receive":
            receives += 1
            if receives == 1:
                return {"state": "waiting", "messages": [], "cursor": 0}
            await worker_reply(broker, answer)
        return await call(ctx, **payload)

    result = await delegate(ctx, reply_after_wait, decide, "worker", task, "locale", mcp_locale=locale)
    assert result["state"] == "completed"
    assert result["answer"]["body"] == answer
    assert broker.channel(result["channel"])["original"] == task
    assert task in ctx.prompts[0] and "worker" in ctx.prompts[0] and result["channel"] in ctx.prompts[0]
    schema = schemas[0]
    assert schema == {"type": "object", "properties": {}}
    english = locale == "en-US"
    assert ctx.prompts[0].replace(task, "").isascii() == english
    assert all(message.isascii() == english for message in ctx.progress)
    assert len(ctx.progress) == 4  # pending, approved, still waiting, completed


@pytest.mark.parametrize("scenario", ["invalid_wait", "incomplete_reply", "invalid_reply", "unavailable", "pending_reply", "invalid_confirmation", "invalid_action"])
async def test_delegate_validation_uses_english_and_keeps_error_codes(broker, scenario):
    ctx = Context()
    arguments = {}
    code = scenario
    if scenario == "invalid_wait":
        arguments["timeout_seconds"] = 0
    elif scenario == "incomplete_reply":
        arguments["reply_to"] = "question"
        code = "invalid_reply"
    elif scenario == "invalid_reply":
        arguments.update(reply_to="", reply="answer")
    elif scenario == "unavailable":
        ctx.client_capabilities = None
        code = "host_confirmation_unavailable"
    elif scenario == "pending_reply":
        arguments.update(reply_to="question", reply="answer")
        code = "invalid_state"
    else:
        async def malformed(**params):
            return SimpleNamespace(action="accept" if scenario == "invalid_confirmation" else "unexpected",
                                   content={"confirmed": "yes"}, meta=None)
        ctx.session.elicit_form = malformed
        code = "invalid_confirmation"
    with pytest.raises(ConnectorError) as failure:
        await delegate(ctx, *operations(broker), "worker", "Task", "invalid", mcp_locale="en-US", **arguments)
    assert failure.value.code == code
    assert str(failure.value).isascii()
    assert not broker.db.execute("SELECT * FROM messages").fetchall()


@pytest.mark.parametrize("scenario", ["decline", "cancel", "approval_timeout", "running", "input_required", "outstanding_question", "decision_conflict"])
async def test_delegate_outcome_details_are_english_and_keep_actual_state(broker, scenario):
    ctx = Context(scenario if scenario in ("decline", "cancel") else "accept")
    ctx.confirmation_meta = {"detail": "宿主原始元数据"}
    call, decide = operations(broker)
    expected = scenario
    if scenario in ("decline", "cancel"):
        expected = "denied"
    if scenario == "approval_timeout":
        async def expired(**params):
            raise TimeoutError
        ctx.session.elicit_form = expired
    elif scenario == "decision_conflict":
        ctx.action = "decline"
        async def accept_before_decline():
            channel = broker.snapshot()["channels"][0]["id"]
            await broker.decide(channel, True, host_approval=("requester", "accept"))
        ctx.on_elicit = accept_before_decline
        expected = "active"

    async def with_worker(ctx, **payload):
        result = await call(ctx, **payload)
        if payload["action"] == "result" and result["state"] == "active":
            if scenario == "running":
                raise TimeoutError
            if scenario in ("input_required", "outstanding_question"):
                question = (await broker.receive("worker", timeout=0))["messages"][0]
                await broker.send("worker", question["channel"], "question", "保留追问原文", "clarify", question["id"])
                if scenario == "outstanding_question":
                    await broker.send("worker", question["channel"], "answer", "保留结果原文", "result", question["id"])
                    result = broker.delegation_result("requester", question["channel"])
        return result

    result = await delegate(ctx, with_worker, decide, "worker", "Task", "outcome", mcp_locale="en-US")
    assert result["state"] == ("input_required" if scenario == "outstanding_question" else expected)
    assert result["detail"].isascii()
    assert all(message.isascii() for message in ctx.progress)
    if scenario in ("decline", "cancel"):
        assert result["decision"] == scenario
        assert result["host_response_meta"] == ctx.confirmation_meta
    if scenario in ("input_required", "outstanding_question"):
        assert result["questions"][0]["body"] == "保留追问原文"
    if scenario == "outstanding_question":
        assert result["answer"]["body"] == "保留结果原文"
    if scenario == "decision_conflict":
        assert result["decision_applied"] is False


async def test_english_supplemental_reply_is_forwarded_without_translation(broker):
    channel = await broker.open("requester", "worker", "Task", "follow-up")
    await broker.decide(channel["id"], True)
    original = (await broker.receive("worker", timeout=0))["messages"][0]
    question = await broker.send("worker", channel["id"], "question", "Which language?", "clarify", original["id"])
    call, decide = operations(broker)

    async def finish_after_reply(ctx, **payload):
        result = await call(ctx, **payload)
        if payload["action"] == "send":
            assert result["body"] == "原样保留此补充回复"
            await broker.send("worker", channel["id"], "answer", "真实结果", "final", original["id"])
        return result

    result = await delegate(Context(), finish_after_reply, decide, "worker", "Task", "follow-up",
                            reply_to=question["id"], reply="原样保留此补充回复", mcp_locale="en-US")
    assert result["answer"]["body"] == "真实结果"


@pytest.mark.parametrize("locale", ["fr-FR", "", None])
async def test_invalid_delegate_locale_fails_before_opening_channel(broker, locale):
    with pytest.raises(ValueError, match="mcp_locale"):
        await delegate(Context(), *operations(broker), "worker", "Task", "locale", mcp_locale=locale)
    assert broker.snapshot()["channels"] == []
