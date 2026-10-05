import asyncio

import pytest

from local_ai_connector.core import Broker, ConnectorError
from local_ai_connector.delegation import continue_task, delegate
from test_delegation import Context, operations


@pytest.fixture
def broker(tmp_path):
    broker = Broker(tmp_path / "state.sqlite3")
    broker.register("requester", "a")
    broker.register("worker", "b")
    yield broker
    broker.close()


async def test_followup_and_answer_complete_same_task_without_repeat_messages(broker):
    ctx = Context()
    call, decide = operations(broker)
    async def ask():
        original = (await broker.receive("worker", timeout=3))["messages"][0]
        question = await broker.send("worker", original["channel"], "question", "Which language?", "clarify", original["id"])
        return original, question
    asking = asyncio.create_task(ask())
    first = await delegate(ctx, call, decide, "worker", "Write a summary", "task", timeout_seconds=1)
    original, question = await asking
    assert first["state"] == "input_required" and first["questions"][0]["id"] == question["id"]
    async def finish():
        messages = (await broker.receive("worker", original["channel"], after=original["seq"], timeout=3))["messages"]
        answer = next(m for m in messages if m["reply_to"] == question["id"])
        assert answer["body"] == "Chinese"
        await broker.send("worker", original["channel"], "answer", "真实中文摘要", "final", original["id"])
    finishing = asyncio.create_task(finish())
    completed = await delegate(ctx, call, decide, "worker", "Write a summary", "task", timeout_seconds=1,
                               reply_to=question["id"], reply="Chinese")
    await finishing
    assert completed["state"] == "completed" and completed["answer"]["body"] == "真实中文摘要"
    repeated = await delegate(ctx, call, decide, "worker", "Write a summary", "task", timeout_seconds=1,
                              reply_to=question["id"], reply="Chinese")
    assert repeated == completed and len(ctx.prompts) == 1
    assert broker.db.execute("SELECT count(*) FROM channels").fetchone()[0] == 1
    assert broker.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 4
    with broker.db:
        broker.db.execute("UPDATE channels SET status='closed' WHERE id=?", (original["channel"],))
    assert await delegate(ctx, call, decide, "worker", "Write a summary", "task", timeout_seconds=1,
                          reply_to=question["id"], reply="Chinese") == completed


async def test_initial_answer_with_outstanding_followup_returns_answerable_question(broker):
    ctx = Context()
    channel = await broker.open("requester", "worker", "Task", "task")
    await broker.decide(channel["id"], True)
    original = (await broker.receive("worker", timeout=0))["messages"][0]
    question = await broker.send("worker", channel["id"], "question", "Anything else?", "clarify", original["id"])
    await broker.send("worker", channel["id"], "answer", "Initial answer", "final", original["id"])
    result = await delegate(ctx, *operations(broker), "worker", "Task", "task", timeout_seconds=1)
    assert result["state"] == "input_required"
    assert result["questions"][0]["id"] == question["id"]
    completed = await delegate(ctx, *operations(broker), "worker", "Task", "task", timeout_seconds=1,
                               reply_to=question["id"], reply="No")
    assert completed["state"] == "completed" and completed["answer"]["body"] == "Initial answer"


async def test_continue_task_reuses_closed_channel_and_answers_round_clarification(broker):
    import time

    ctx = Context()
    call, _ = operations(broker)
    channel = await broker.open("requester", "worker", "Original task", "original")
    await broker.decide(channel["id"], True)
    initial = (await broker.receive("worker", channel["id"], timeout=0))["messages"][0]
    await broker.send("worker", channel["id"], "answer", "Initial result", "initial-result", initial["id"])
    await broker.finish("worker", channel["id"])
    with broker.db:
        broker.db.execute("UPDATE channels SET status='closed', idle_since=? WHERE id=?",
                          (time.time() - 1, channel["id"]))

    async def worker_round():
        request = (await broker.receive("worker", channel["id"], after=initial["seq"], timeout=3))["messages"][0]
        clarification = await broker.send("worker", channel["id"], "question", "One detail?", "round-detail",
                                           request["id"])
        response = (await broker.receive("worker", channel["id"], after=clarification["seq"], timeout=3))["messages"][0]
        assert response["reply_to"] == clarification["id"] and response["body"] == "The detail"
        return await broker.send("worker", channel["id"], "answer", "Follow-up result", "round-answer",
                                 request["id"])

    worker = asyncio.create_task(worker_round())
    first = await continue_task(ctx, call, channel["id"], "Continue the same task", "round-2", timeout_seconds=2)
    assert first["state"] == "input_required" and first["questions"][0]["body"] == "One detail?"
    completed = await continue_task(ctx, call, channel["id"], "Continue the same task", "round-2",
                                    timeout_seconds=2, reply_to=first["questions"][0]["id"], reply="The detail")
    await worker
    assert completed["state"] == "completed" and completed["answer"]["body"] == "Follow-up result"
    repeated = await continue_task(ctx, call, channel["id"], "Continue the same task", "round-2", timeout_seconds=1)
    assert repeated == completed
    with broker.db:
        broker.db.execute("UPDATE channels SET status='closed', idle_since=? WHERE id=?",
                          (time.time() - 1, channel["id"]))
    closed_replay = await continue_task(ctx, call, channel["id"], "Continue the same task", "round-2",
                                        timeout_seconds=1, reply_to=first["questions"][0]["id"], reply="The detail")
    assert closed_replay == completed
    assert broker.db.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1
    assert broker.db.execute("SELECT COUNT(*) FROM messages WHERE sender='requester' AND kind='question'").fetchone()[0] == 2


async def test_cancelled_followup_send_can_resume_without_another_answer(broker):
    channel = await broker.open("requester", "worker", "Task", "task")
    await broker.decide(channel["id"], True)
    original = (await broker.receive("worker", timeout=0))["messages"][0]
    question = await broker.send("worker", channel["id"], "question", "Which language?", "clarify", original["id"])
    call, decide = operations(broker)
    async def interrupted(ctx, **payload):
        result = await call(ctx, **payload)
        if payload["action"] == "send":
            raise asyncio.CancelledError()
        return result
    with pytest.raises(asyncio.CancelledError):
        await delegate(Context(), interrupted, decide, "worker", "Task", "task", timeout_seconds=1,
                       reply_to=question["id"], reply="Chinese")
    await broker.send("worker", channel["id"], "answer", "真实结果", "final", original["id"])
    result = await delegate(Context(), call, decide, "worker", "Task", "task", timeout_seconds=1,
                            reply_to=question["id"], reply="Chinese")
    assert result["state"] == "completed"
    assert broker.db.execute("SELECT count(*) FROM messages WHERE reply_to=? AND kind='answer'", (question["id"],)).fetchone()[0] == 1
    with pytest.raises(ConnectorError) as failure:
        await delegate(Context(), call, decide, "worker", "Task", "task", timeout_seconds=1,
                       reply_to=question["id"], reply="Changed answer")
    assert failure.value.code == "idempotency_conflict"


async def test_revocation_during_followup_lookup_returns_terminal_state(broker):
    channel = await broker.open("requester", "worker", "Task", "task")
    await broker.decide(channel["id"], True)
    original = (await broker.receive("worker", timeout=0))["messages"][0]
    await broker.send("worker", channel["id"], "question", "Anything else?", "clarify", original["id"])
    await broker.send("worker", channel["id"], "answer", "Original result", "final", original["id"])
    call, decide = operations(broker)
    async def revoke_before_lookup(ctx, **payload):
        if payload["action"] == "receive":
            await broker.revoke(channel["id"])
        return await call(ctx, **payload)
    result = await delegate(Context(), revoke_before_lookup, decide, "worker", "Task", "task", timeout_seconds=1)
    assert result == {"state": "revoked", "channel": channel["id"]}


@pytest.mark.parametrize("reference", ["initial", "other_channel", "missing"])
async def test_followup_cannot_answer_wrong_question(broker, reference):
    channel = await broker.open("requester", "worker", "Task", "task")
    await broker.decide(channel["id"], True)
    original = (await broker.receive("worker", channel["id"], timeout=0))["messages"][0]
    if reference == "initial":
        reply_to = original["id"]
    elif reference == "missing":
        reply_to = "not-a-question"
    else:
        other = await broker.open("requester", "worker", "Other task", "other")
        await broker.decide(other["id"], True)
        reply_to = (await broker.send("worker", other["id"], "question", "Other clarification", "other-q"))["id"]
    with pytest.raises(ConnectorError) as failure:
        await delegate(Context(), *operations(broker), "worker", "Task", "task", timeout_seconds=1,
                       reply_to=reply_to, reply="answer")
    assert failure.value.code == "wrong_reply"
    assert broker.db.execute("SELECT count(*) FROM messages WHERE sender='requester' AND kind='answer'").fetchone()[0] == 0


@pytest.mark.parametrize("reply_to,reply", [("id", None), (None, "answer"), ("", "answer"), ("id", "")])
async def test_followup_arguments_are_validated_before_creating_task(broker, reply_to, reply):
    with pytest.raises(ConnectorError) as failure:
        await delegate(Context(), *operations(broker), "worker", "Task", "task",
                       reply_to=reply_to, reply=reply)
    assert failure.value.code == "invalid_reply"
    assert broker.snapshot()["channels"] == []


async def test_approval_timeout_observes_concurrent_approval_and_real_result(broker):
    ctx = Context()
    async def approve_elsewhere():
        channel = broker.snapshot()["channels"][0]
        await broker.decide(channel["id"], True)
        original = (await broker.receive("worker", timeout=0))["messages"][0]
        await broker.send("worker", channel["id"], "answer", "Concurrent result", "final", original["id"])
        raise TimeoutError()
    ctx.on_elicit = approve_elsewhere
    result = await delegate(ctx, *operations(broker), "worker", "Task", "task", timeout_seconds=1)
    assert result["state"] == "completed" and result["answer"]["body"] == "Concurrent result"


async def test_decline_after_concurrent_approval_reports_conflict_without_revocation(broker):
    ctx = Context(action="decline")
    async def approve_elsewhere():
        await broker.decide(broker.snapshot()["channels"][0]["id"], True)
    ctx.on_elicit = approve_elsewhere
    result = await delegate(ctx, *operations(broker), "worker", "Task", "task", timeout_seconds=1)
    assert result["state"] == "active" and result["decision"] == "decline"
    assert result["decision_applied"] is False
    assert broker.snapshot()["channels"][0]["status"] == "active"


@pytest.mark.parametrize("operation", ["result", "finish"])
async def test_execution_deadline_also_bounds_result_and_finish(broker, operation):
    ctx = Context()
    call, decide = operations(broker)
    cancelled = False
    if operation == "finish":
        channel = await broker.open("requester", "worker", "Task", "task")
        await broker.decide(channel["id"], True)
        question = (await broker.receive("worker", timeout=0))["messages"][0]
        await broker.send("worker", channel["id"], "answer", "Result", "final", question["id"])
    async def hold(ctx, **payload):
        nonlocal cancelled
        if payload["action"] == operation:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled = True
                raise
        return await call(ctx, **payload)
    result = await asyncio.wait_for(delegate(ctx, hold, decide, "worker", "Task", "task", timeout_seconds=1), 1.3)
    assert result["state"] == "running" and result["request_key"] == "task" and cancelled


async def paged_followups(broker, resolved_count):
    channel = await broker.open("requester", "worker", "Task", "paged")
    await broker.decide(channel["id"], True)
    original = (await broker.receive("worker", channel["id"], timeout=0))["messages"][0]
    for index in range(resolved_count):
        question = await broker.send("worker", channel["id"], "question", f"Clarification {index}", f"question-{index}")
        await broker.send("requester", channel["id"], "answer", "Answered", f"reply-{index}", question["id"])
    pending = await broker.send("worker", channel["id"], "question", "Remaining clarification", "remaining")
    await broker.send("worker", channel["id"], "answer", "Initial result", "initial-answer", original["id"])
    return channel, pending


@pytest.mark.parametrize("resolved_count", [100, 200])
async def test_initial_answer_finds_pending_followup_beyond_first_page(broker, resolved_count):
    channel, pending = await paged_followups(broker, resolved_count)
    for _ in range(2):
        result = await delegate(Context(), *operations(broker), "worker", "Task", "paged", timeout_seconds=1)
        assert result["state"] == "input_required"
        assert [question["id"] for question in result["questions"]] == [pending["id"]]
        assert result["channel"] == channel["id"]


async def test_revocation_between_followup_pages_returns_no_content(broker):
    channel, _ = await paged_followups(broker, 100)
    call, decide = operations(broker)
    async def revoke_before_second_page(ctx, **payload):
        if payload["action"] == "receive" and payload["after"] > 0:
            await broker.revoke(channel["id"])
        return await call(ctx, **payload)
    result = await delegate(Context(), revoke_before_second_page, decide, "worker", "Task", "paged", timeout_seconds=1)
    assert result == {"state": "revoked", "channel": channel["id"]}


async def test_followup_resolved_while_paging_finishes_without_empty_input_request(broker):
    _, pending = await paged_followups(broker, 100)
    call, decide = operations(broker)
    resolved = False
    async def resolve_before_second_page(ctx, **payload):
        nonlocal resolved
        if payload["action"] == "receive" and payload["after"] > 0 and not resolved:
            resolved = True
            await broker.send("requester", pending["channel"], "answer", "Answered concurrently", "concurrent", pending["id"])
        return await call(ctx, **payload)
    result = await delegate(Context(), resolve_before_second_page, decide, "worker", "Task", "paged", timeout_seconds=1)
    assert result["state"] == "completed" and result["answer"]["body"] == "Initial result"


async def test_followup_pagination_obeys_execution_deadline(broker):
    await paged_followups(broker, 100)
    call, decide = operations(broker)
    cancelled = False
    async def hold_second_page(ctx, **payload):
        nonlocal cancelled
        if payload["action"] == "receive" and payload["after"] > 0:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled = True
                raise
        return await call(ctx, **payload)
    result = await asyncio.wait_for(delegate(Context(), hold_second_page, decide, "worker", "Task", "paged", timeout_seconds=1), 1.3)
    assert cancelled and result["state"] == "running" and result["request_key"] == "paged"


async def test_same_key_closed_replay_after_expiry_returns_no_answer(broker):
    now = [1000]
    broker.clock = lambda: now[0]
    channel = await broker.open("requester", "worker", "Task", "closed")
    await broker.decide(channel["id"], True)
    question = (await broker.receive("worker", channel["id"], timeout=0))["messages"][0]
    await broker.send("worker", channel["id"], "answer", "Private result", "final", question["id"])
    ctx = Context()
    before = await delegate(ctx, *operations(broker), "worker", "Task", "closed", timeout_seconds=1)
    assert before["state"] == "completed"
    now[0] = 1121
    assert broker.delegation_result("requester", channel["id"])["state"] == "closed"
    now[0] = 4600
    after = await delegate(ctx, *operations(broker), "worker", "Task", "closed", timeout_seconds=1)
    assert after == {"state": "expired", "channel": channel["id"]}
    assert ctx.prompts == []
    assert broker.channel(channel["id"])["status"] == "closed"
    assert broker.db.execute("SELECT count(*) FROM channels").fetchone()[0] == 1
