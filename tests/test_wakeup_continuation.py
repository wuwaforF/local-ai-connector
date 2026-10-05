"""An idle worker must be resumed for a clarification answer, without replaying work."""
from types import SimpleNamespace

import pytest

from local_ai_connector.delegation import delegate
from local_ai_connector.wakeup import Binding
from test_delegation import Context
from test_wakeup import FakeHost, TARGET, approved, env  # noqa: F401: shared pytest fixture


async def waiting_for_user(env):
    dispatcher = env.dispatcher()
    channel = await approved(env.broker, "continuation-task", body="Write a short report")
    await dispatcher.step()
    original = (await env.broker.receive("zcode", timeout=0))["messages"][0]
    question = await env.broker.send(
        "zcode", channel, "question", "Which language?", "clarification", original["id"])
    return SimpleNamespace(dispatcher=dispatcher, channel=channel, original=original,
                           question=question, original_dispatch=env.host.sent[0][0])


async def supplement(env, task):
    return await env.broker.send(
        "asker", task.channel, "answer", "Chinese", f"delegate-reply:{task.question['id']}",
        task.question["id"])


async def consume_supplement(env, task):
    received = await env.broker.receive("zcode", task.channel, after=task.original["seq"], timeout=0)
    assert [m["body"] for m in received["messages"]] == ["Chinese"]
    return received["messages"][0]


def delegate_operations(env, after_send=None):
    async def call(ctx, **p):
        action = p["action"]
        if action == "open":
            return await env.broker.open("asker", p["target"], p["body"], p["key"],
                                         p["ttl_seconds"], p["idle_seconds"])
        if action == "result":
            return env.broker.delegation_result("asker", p["channel"])
        if action == "receive":
            return await env.broker.receive("asker", p["channel"], p["after"], p["timeout"])
        if action == "finish":
            return await env.broker.finish("asker", p["channel"])
        if action == "send":
            result = await env.broker.send("asker", p["channel"], p["kind"], p["body"], p["key"], p["reply_to"])
            if after_send:
                await after_send()
            return result
        raise AssertionError(action)

    async def decide(ctx, channel, decision):
        raise AssertionError("Resuming an approved task must not request approval again")

    return call, decide


def dispatch_for(env, message_id):
    rows = [r for r in env.store.report() if message_id in env.store.messages(r["id"])]
    assert len(rows) == 1
    return rows[0]


async def test_delegate_clarification_resumes_idle_worker_and_completed_replay_is_inert(env):
    task = await waiting_for_user(env)
    ctx = Context()
    args = ("zcode", "Write a short report", "continuation-task")
    first = await delegate(ctx, *delegate_operations(env), *args, timeout_seconds=1)
    assert first["state"] == "input_required"
    assert first["questions"][0]["id"] == task.question["id"]

    async def resumed_worker():
        await consume_supplement(env, task)
        await env.broker.send("zcode", task.channel, "answer", "Final Chinese report", "final", task.original["id"])

    env.host.during_send = resumed_worker
    completed = await delegate(ctx, *delegate_operations(env, task.dispatcher.step), *args,
                               timeout_seconds=1, reply_to=task.question["id"], reply="Chinese")
    assert completed["state"] == "completed"
    assert completed["answer"]["body"] == "Final Chinese report"
    await task.dispatcher.step()
    reply = await supplement(env, task)
    continuation = dispatch_for(env, reply["id"])
    assert len(env.host.sent) == 2 and continuation["id"] != task.original_dispatch
    assert env.store.state(task.original_dispatch) == "replied"
    assert continuation["state"] == "delivered"
    assert continuation["events"][-1]["stage"] == "late_acknowledged"

    before = env.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    repeated = await delegate(ctx, *delegate_operations(env, task.dispatcher.step), *args,
                              timeout_seconds=1, reply_to=task.question["id"], reply="Chinese")
    assert repeated == completed and ctx.prompts == []
    assert env.broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before
    assert len(env.host.sent) == 2


async def test_user_wait_does_not_timeout_and_consuming_reply_restarts_durable_turn_timer(env):
    task = await waiting_for_user(env)
    env.now[0] += 61
    await task.dispatcher.step()
    assert env.store.state(task.original_dispatch) == "tool_executed"
    assert env.broker.snapshot()["incidents"] == []

    reply = await supplement(env, task)
    await task.dispatcher.step()
    assert len(env.host.sent) == 2
    env.now[0] += 5
    await consume_supplement(env, task)
    assert dispatch_for(env, reply["id"])["state"] == "delivered"

    recovered = env.reopen()
    env.now[0] += 59
    await recovered.step()
    assert env.store.state(task.original_dispatch) == "tool_executed"
    env.now[0] += 2
    await recovered.step()
    assert env.store.state(task.original_dispatch) == "no_reply_observed"
    assert len(env.host.sent) == 2


@pytest.mark.parametrize("received", [False, True], ids=["acknowledged", "delivered"])
async def test_replaying_clarification_after_restart_never_sends_it_again(env, received):
    task = await waiting_for_user(env)
    reply = await supplement(env, task)
    await task.dispatcher.step()
    assert len(env.host.sent) == 2
    if received:
        await consume_supplement(env, task)
    recovered = env.reopen()
    assert (await supplement(env, task))["id"] == reply["id"]
    for _ in range(3):
        await recovered.step()
    assert len(env.host.sent) == 2
    assert dispatch_for(env, reply["id"])["state"] == ("delivered" if received else "acknowledged")


@pytest.mark.parametrize("boundary", ["busy", "unknown", "revoked", "expired", "initial_answered"])
async def test_continuation_obeys_host_and_authorization_boundaries(env, boundary):
    task = await waiting_for_user(env)
    await supplement(env, task)
    if boundary in ("busy", "unknown"):
        env.host.state = boundary
    elif boundary == "revoked":
        await env.broker.revoke(task.channel)
    elif boundary == "expired":
        env.now[0] += 3601
    else:
        await env.broker.send("zcode", task.channel, "answer", "Already done", "final", task.original["id"])
    await task.dispatcher.step()
    assert len(env.host.sent) == 1
    if boundary in ("busy", "unknown"):
        env.host.state = "idle"
        env.now[0] += 11
        await task.dispatcher.step()
        assert len(env.host.sent) == 2


async def test_ordinary_final_answer_does_not_wake_a_bound_requester(env):
    dispatcher = env.dispatcher()
    asker_host = FakeHost()
    dispatcher.bindings["asker"] = Binding("asker", dict(TARGET), asker_host)
    channel = await approved(env.broker)
    await dispatcher.step()
    original = (await env.broker.receive("zcode", timeout=0))["messages"][0]
    await env.broker.send("zcode", channel, "answer", "Final answer", "final", original["id"])
    await dispatcher.step()
    assert asker_host.sent == [] and asker_host.calls == []
    assert len(env.host.sent) == 1


@pytest.mark.parametrize("when", ["before_gate", "during_gate", "before_retry"])
async def test_reply_already_received_cannot_trigger_a_send_or_retry(env, when):
    task = await waiting_for_user(env)
    reply = await supplement(env, task)
    if when == "before_gate":
        await consume_supplement(env, task)
    elif when == "during_gate":
        original_confirm = env.host.confirm
        consumed = []

        async def receive_during_confirm(target):
            result = await original_confirm(target)
            consumed.append(await consume_supplement(env, task))
            return result

        env.host.confirm = receive_during_confirm
    else:
        env.host.send_mode = "rejected_retryable"
        await task.dispatcher.step()
        assert len(env.host.sent) == 2
        assert dispatch_for(env, reply["id"])["state"] == "retry_pending"
        await consume_supplement(env, task)
        env.now[0] += 11

    await task.dispatcher.step()
    assert len(env.host.sent) == (2 if when == "before_retry" else 1)
    if when == "during_gate":
        assert len(consumed) == 1
    if when == "before_retry":
        assert dispatch_for(env, reply["id"])["state"] == "delivered"


async def test_two_clarification_rounds_each_wake_once_and_replays_are_inert(env):
    task = await waiting_for_user(env)
    ctx = Context()
    args = ("zcode", "Write a short report", "continuation-task")
    cursor = task.original["seq"]
    replies = []

    async def next_worker_turn():
        nonlocal cursor
        got = await env.broker.receive("zcode", task.channel, after=cursor, timeout=0)
        cursor = got["cursor"]
        assert len(got["messages"]) == 1
        replies.append(got["messages"][0])
        if len(replies) == 1:
            assert replies[-1]["body"] == "Chinese"
            await env.broker.send("zcode", task.channel, "question", "How long?", "clarification-2", task.original["id"])
        else:
            assert replies[-1]["body"] == "100 words"
            await env.broker.send("zcode", task.channel, "answer", "Final report", "final", task.original["id"])

    env.host.during_send = next_worker_turn
    ops = delegate_operations(env, task.dispatcher.step)
    second_question = await delegate(ctx, *ops, *args, timeout_seconds=1,
                                     reply_to=task.question["id"], reply="Chinese")
    assert second_question["state"] == "input_required"
    assert len(env.host.sent) == 2
    assert await delegate(ctx, *ops, *args, timeout_seconds=1,
                          reply_to=task.question["id"], reply="Chinese") == second_question
    assert len(env.host.sent) == 2

    reference = second_question["questions"][0]["id"]
    completed = await delegate(ctx, *ops, *args, timeout_seconds=1, reply_to=reference, reply="100 words")
    assert completed["state"] == "completed" and completed["answer"]["body"] == "Final report"
    assert await delegate(ctx, *ops, *args, timeout_seconds=1,
                          reply_to=reference, reply="100 words") == completed
    await task.dispatcher.step()
    assert len(env.host.sent) == 3 and len({did for did, _ in env.host.sent}) == 3
    assert env.store.state(task.original_dispatch) == "replied"
    assert [dispatch_for(env, reply["id"])["state"] for reply in replies] == ["delivered", "delivered"]
    assert all(row["attempts"] == 1 for row in env.store.report())


async def test_second_task_round_clarification_reply_wakes_the_same_worker_binding(env):
    dispatcher = env.dispatcher()
    channel = await approved(env.broker, "round-two-wake", body="Original task")
    await dispatcher.step()
    original = (await env.broker.receive("zcode", channel, timeout=0))["messages"][0]
    await env.broker.send("zcode", channel, "answer", "First result", "first-result", original["id"])
    await env.broker.finish("zcode", channel)
    env.now[0] += 121
    env.broker.expire()

    continuation = await env.broker.continue_channel("asker", channel, "Continue the same task", "round-two")
    await dispatcher.step()
    assert len(env.host.sent) == 2
    delivered = (await env.broker.receive("zcode", channel, after=original["seq"], timeout=0))["messages"]
    assert [message["id"] for message in delivered] == [continuation["id"]]
    clarification = await env.broker.send("zcode", channel, "question", "One more detail?", "ordinary-clarification",
                                          continuation["id"])
    await dispatcher.step()
    answer = await env.broker.send("asker", channel, "answer", "The detail", "round-two-answer",
                                   clarification["id"])
    await dispatcher.step()
    assert len(env.host.sent) == 3
    assert env.host.sent[-1][0] != env.host.sent[-2][0]
    assert env.store.channel_target(channel, "zcode")



async def test_retryable_continuation_reply_stops_after_parent_round_resolves(env):
    dispatcher = env.dispatcher()
    channel = await approved(env.broker, "retry-continuation", body="Original task")
    await dispatcher.step()
    original = (await env.broker.receive("zcode", channel, timeout=0))["messages"][0]
    await env.broker.send("zcode", channel, "answer", "First result", "first-result", original["id"])
    await env.broker.finish("zcode", channel)
    env.now[0] += 121
    env.broker.expire()

    continuation = await env.broker.continue_channel("asker", channel, "Continue this task", "round-two")
    await dispatcher.step()
    await env.broker.receive("zcode", channel, after=original["seq"], timeout=0)
    clarification = await env.broker.send(
        "zcode", channel, "question", "One more detail?", "ordinary-clarification", continuation["id"])
    answer = await env.broker.send(
        "asker", channel, "answer", "The detail", "round-two-answer", clarification["id"])
    env.host.send_mode = "rejected_retryable"
    await dispatcher.step()
    assert len(env.host.sent) == 3
    queued = dispatch_for(env, answer["id"])
    assert queued["state"] == "retry_pending"

    await env.broker.send("zcode", channel, "answer", "Finished", "round-two-final", continuation["id"])
    env.now[0] += 11
    await dispatcher.step()
    assert len(env.host.sent) == 3
    assert dispatch_for(env, answer["id"])["state"] == "authority_ended"


@pytest.mark.parametrize("include_original", [False, True], ids=["new_cursor", "after_zero"])
async def test_consumed_reply_replay_after_restart_cannot_extend_deadline(env, include_original):
    task = await waiting_for_user(env)
    env.now[0] += 61
    await task.dispatcher.step()
    reply = await supplement(env, task)
    await task.dispatcher.step()
    env.now[0] += 5
    got = await env.broker.receive("zcode", task.channel,
                                   after=0 if include_original else task.original["seq"], timeout=0)
    assert reply["id"] in {m["id"] for m in got["messages"]}
    resumed_at = env.now[0]
    assert dispatch_for(env, task.original["id"])["updated"] == resumed_at

    recovered = env.reopen()
    env.now[0] += 59
    await env.broker.receive("zcode", task.channel, after=0, timeout=0)
    assert dispatch_for(env, task.original["id"])["updated"] == resumed_at
    await recovered.step()
    assert env.store.state(task.original_dispatch) == "tool_executed"

    recovered = env.reopen()
    env.now[0] += 2
    await recovered.step()
    assert env.store.state(task.original_dispatch) == "no_reply_observed"
    assert len(env.host.sent) == 2


async def test_bound_requester_cannot_treat_its_initial_task_as_waiting_for_input(env):
    task = await waiting_for_user(env)
    asker_host = FakeHost()
    task.dispatcher.bindings["asker"] = Binding("asker", dict(TARGET), asker_host)
    await task.dispatcher.step()
    assert len(asker_host.sent) == 1
    received = await env.broker.receive("asker", task.channel, timeout=0)
    assert [m["id"] for m in received["messages"]] == [task.question["id"]]
    asker_dispatch = asker_host.sent[0][0]
    assert env.store.state(asker_dispatch) == "tool_executed"

    env.now[0] += 61
    await task.dispatcher.step()
    assert env.store.state(asker_dispatch) == "no_reply_observed"
    assert env.store.state(task.original_dispatch) == "tool_executed"
