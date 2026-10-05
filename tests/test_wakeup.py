"""Synthetic tests for the optional wake-up dispatcher. No real host is contacted.

FakeHost stands in for a host adapter. Passing these tests shows the connector-side state
boundaries hold; it does not show that ZCode or any other real host can be activated.
"""
import asyncio
import json
import os
import sys

import httpx
import pytest

from local_ai_connector.core import Broker
from local_ai_connector.server import create_app
from local_ai_connector.wakeup import (AdapterError, Binding, CommandAdapter, Dispatcher, WakeAdapter,
                                       WakeConfigError, WakeStore, load_config)

TARGET = {"workspace_key": "synthetic-workspace", "task_id": "synthetic-task"}
SECRET_BODY = "SECRET-BODY-7f3a what is 17+25?"


class FakeHost(WakeAdapter):
    def __init__(self):
        self.state, self.identity, self.sent, self.calls = "idle", dict(TARGET), [], []
        self.send_mode, self.reconcile_result, self.during_send = "ack", "unknown", None
        self.during_reconcile = None

    async def confirm(self, target):
        self.calls.append("confirm")
        return self.identity

    async def status(self, target):
        self.calls.append("status")
        return self.state

    async def send(self, target, dispatch_id, text):
        self.sent.append((dispatch_id, text))
        if self.during_send:
            await self.during_send()
        if self.send_mode == "cancel_after_effect":
            # The host-side effect has happened (recorded above); the acknowledgment never arrives.
            raise asyncio.CancelledError()
        if self.send_mode == "hang":
            await asyncio.sleep(3600)
        if self.send_mode == "rejected":
            raise AdapterError("rejected", "synthetic")
        if self.send_mode == "rejected_retryable":
            raise AdapterError("rejected", "synthetic", retryable=True)
        if self.send_mode == "owner_discovery_timeout":
            raise AdapterError("owner_discovery_timeout", "synthetic owner discovery timeout", retryable=True)
        if self.send_mode == "malformed":
            raise AdapterError("ambiguous", "synthetic malformed reply")
        return {"host_ref": "synthetic-ref"}

    async def reconcile(self, target, dispatch_id):
        self.calls.append("reconcile")
        if self.during_reconcile:
            await self.during_reconcile()
        return self.reconcile_result


@pytest.fixture
def env(tmp_path):
    now = [1000.0]
    broker = Broker(tmp_path / "state.sqlite3", clock=lambda: now[0])
    for peer in ("asker", "zcode", "other"):
        broker.register(peer, peer + "-token")
    e = type("Env", (), {})()
    e.broker, e.host, e.now, e.path = broker, FakeHost(), now, tmp_path
    e.store = WakeStore(tmp_path / "wakeup.sqlite3")

    def dispatcher(**options):
        options = {"settle_seconds": 0, "busy_retry_seconds": 10, "ack_timeout": 0.05, "turn_timeout": 60, **options}
        return Dispatcher(broker, e.store, {"zcode": Binding("zcode", dict(TARGET), e.host)}, clock=lambda: now[0], **options)

    def reopen(**options):
        """Simulate a process restart: drop the old dispatcher, close and reopen wakeup.sqlite3."""
        broker.observers.clear()
        e.store.close()
        e.store = WakeStore(tmp_path / "wakeup.sqlite3")
        return dispatcher(**options)

    e.dispatcher, e.reopen = dispatcher, reopen
    yield e
    e.store.close()
    broker.close()


async def approved(broker, key="k1", body=SECRET_BODY, **kw):
    channel = await broker.open("asker", "zcode", body, key, **kw)
    await broker.decide(channel["id"], True)
    return channel["id"]


async def test_stale_workflow_does_not_suspend_or_consume_unrelated_task(env):
    identities = {"zcode": "builder-A", "other": "reviewer-A"}
    env.broker.workflows.identity = identities.get
    run = await env.broker.workflows.start("asker", "Small stored text artifact", "zcode", "other", "mixed-workflow")
    stale_channel = run["stages"][0]["channel"]
    await env.broker.decide(stale_channel, True)
    ordinary = await approved(env.broker, key="ordinary-after-workflow")
    identities["other"] = "reviewer-B"
    dispatcher = env.dispatcher()
    await dispatcher.step()
    assert len(env.host.sent) == 1
    dispatch_id = env.host.sent[0][0]
    included = env.store.messages(dispatch_id)
    assert {env.broker.db.execute("SELECT channel FROM messages WHERE id=?", (mid,)).fetchone()[0]
            for mid in included} == {ordinary}
    assert dispatcher.bindings["zcode"].suspended is None
    received = await env.broker.receive("zcode", timeout=0)
    assert {message["channel"] for message in received["messages"]} == {ordinary}
    assert received["blocked_channels"] == [{"channel": stale_channel, "error": "workflow_binding_changed"}]
    assert env.store.channel_target(stale_channel, "zcode") is None


async def test_workflow_binding_change_during_gate_keeps_other_channels_eligible(env):
    identities = {"zcode": "builder-A", "other": "reviewer-A"}
    env.broker.workflows.identity = identities.get
    run = await env.broker.workflows.start("asker", "Small stored text artifact", "zcode", "other", "gate-workflow")
    await env.broker.decide(run["stages"][0]["channel"], True)
    dispatcher = env.dispatcher()
    original_status = env.host.status
    async def changed_status(target):
        identities["other"] = "reviewer-B"
        return await original_status(target)
    env.host.status = changed_status
    await dispatcher.step()
    assert env.host.sent == [] and dispatcher.bindings["zcode"].suspended is None
    ordinary = await approved(env.broker, key="ordinary-after-gate")
    await dispatcher.step()
    assert len(env.host.sent) == 1
    received = await env.broker.receive("zcode", timeout=0)
    assert {message["channel"] for message in received["messages"]} == {ordinary}


@pytest.mark.parametrize("mode", ["rejected_retryable", "malformed"])
async def test_mixed_workflow_batch_requeues_only_when_definitively_undelivered(env, mode):
    identities = {"zcode": "builder-A", "other": "reviewer-A"}
    env.broker.workflows.identity = identities.get
    run = await env.broker.workflows.start("asker", "Small stored text artifact", "zcode", "other", "retry-workflow")
    workflow_channel = run["stages"][0]["channel"]
    await env.broker.decide(workflow_channel, True)
    ordinary = await approved(env.broker, key="ordinary-in-batch")
    dispatcher = env.dispatcher()
    env.host.send_mode = mode
    await dispatcher.step()
    first_id = env.host.sent[0][0]
    original_ids = set(env.store.messages(first_id))
    assert len(original_ids) == 2
    identities["other"] = "reviewer-B"
    env.host.send_mode = "ack"
    env.now[0] += 10
    await dispatcher.step()
    assert dispatcher.bindings["zcode"].suspended is None
    if mode == "rejected_retryable":
        assert len(env.host.sent) == 1
        env.now[0] += 10
        await dispatcher.step()
        assert len(env.host.sent) == 2
        assert env.host.sent[1][0] == first_id
        second_ids = env.store.messages(env.host.sent[1][0])
        assert len(second_ids) == 1
        assert env.broker.db.execute("SELECT channel FROM messages WHERE id=?", (second_ids[0],)).fetchone()[0] == ordinary
        old = next(row for row in env.store.report() if row["id"] == first_id)
        assert set(old["quarantined_messages"]) == original_ids - set(second_ids)
        assert old["state"] == "acknowledged" and old["attempts"] == 2
        assert original_ids <= env.store.covered()
    else:
        assert len(env.host.sent) == 1
        assert original_ids <= env.store.covered()
        assert all("quarantined_messages" not in row for row in env.store.report())


async def test_unsent_intent_quarantines_stale_members_before_first_effect(env):
    identities = {"zcode": "builder-A", "other": "reviewer-A"}
    env.broker.workflows.identity = identities.get
    run = await env.broker.workflows.start("asker", "Small stored text artifact", "zcode", "other", "unsent-workflow")
    await env.broker.decide(run["stages"][0]["channel"], True)
    ordinary = await approved(env.broker, key="ordinary-unsent")
    ids = [row[0] for row in env.broker.db.execute("SELECT id FROM messages ORDER BY seq")]
    env.store.pin_channels({run["stages"][0]["channel"], ordinary}, "zcode", TARGET)
    dispatch_id = env.store.create("zcode", TARGET, ids, 0, env.now[0])
    identities["other"] = "reviewer-B"
    dispatcher = env.dispatcher()
    await dispatcher.step()
    assert env.host.sent == [] and env.store.state(dispatch_id) == "retry_pending"
    env.now[0] += 10
    await dispatcher.step()
    assert len(env.host.sent) == 1 and env.host.sent[0][0] == dispatch_id
    row = next(row for row in env.store.report() if row["id"] == dispatch_id)
    assert row["attempts"] == 1 and len(row["quarantined_messages"]) == 1


async def test_quarantined_batch_preserves_attempt_budget_and_audit_across_restart(env):
    identities = {"zcode": "builder-A", "other": "reviewer-A"}
    env.broker.workflows.identity = identities.get
    run = await env.broker.workflows.start("asker", "Small stored text artifact", "zcode", "other", "budget-workflow")
    await env.broker.decide(run["stages"][0]["channel"], True)
    await approved(env.broker, key="ordinary-budget")
    dispatcher = env.dispatcher(max_attempts=2)
    env.host.send_mode = "rejected_retryable"
    await dispatcher.step()
    dispatch_id = env.host.sent[0][0]
    identities["other"] = "reviewer-B"
    await dispatcher.step()
    dispatcher = env.reopen(max_attempts=2)
    env.now[0] += 10
    await dispatcher.step()
    env.now[0] += 10
    await dispatcher.step()
    assert len(env.host.sent) == 2 and all(did == dispatch_id for did, _ in env.host.sent)
    row = next(row for row in env.store.report() if row["id"] == dispatch_id)
    assert row["state"] == "failed" and row["attempts"] == 2
    assert len(row["quarantined_messages"]) == 1
    assert len(env.store.covered()) == 2


def states(store):
    return [(d["state"], [e["stage"] for e in d["events"]]) for d in reversed(store.report())]


async def test_no_activation_before_approval_then_staged_round_trip(env):
    d = env.dispatcher()
    channel = await env.broker.open("asker", "zcode", SECRET_BODY, "k1")
    await d.step()
    assert env.host.calls == [] and env.host.sent == []  # unapproved: host not even contacted
    await env.broker.decide(channel["id"], True)
    await d.step()
    (dispatch_id, text), = env.host.sent
    assert "SECRET" not in text and dispatch_id in text and "connector_receive" in text and len(text) < 600
    assert states(env.store) == [("acknowledged", ["intent", "attempt_started", "acknowledged"])]  # ack is not success
    got = await env.broker.receive("zcode", after=0, timeout=0)
    assert states(env.store)[0][0] == "tool_executed"
    await env.broker.send("zcode", channel["id"], "answer", "42 MARK", "a1", got["messages"][0]["id"])
    await d.step()
    assert states(env.store)[0] == ("replied", ["intent", "attempt_started", "acknowledged", "tool_executed", "replied"])
    assert len(env.host.sent) == 1


async def test_duplicate_steps_and_multiple_messages_coalesce_into_one_wake(env):
    d = env.dispatcher()
    await approved(env.broker, "k1")
    await approved(env.broker, "k2", body="second")
    for _ in range(5):
        await d.step()
    assert len(env.host.sent) == 1 and len(env.store.messages(env.host.sent[0][0])) == 2
    await approved(env.broker, "k3", body="third")  # arrives while a dispatch is open
    await d.step()
    assert len(env.host.sent) == 1  # no second turn is queued on top of the open one


async def test_settle_revocation_and_expiry_before_dispatch(env):
    d = env.dispatcher(settle_seconds=5)
    revoked = await approved(env.broker, "k1")
    await d.step()
    assert env.host.sent == []  # still settling
    await env.broker.revoke(revoked)
    expiring = await approved(env.broker, "k2", ttl_seconds=10)
    env.now[0] += 11
    await d.step()
    assert env.host.sent == [] and env.store.report() == []
    assert env.broker.channel(expiring)["status"] == "expired"


async def run_counting_steps(d):
    steps, original = [], d.step

    async def counted():
        steps.append(None)
        await original()
    d.step = counted
    return steps, asyncio.create_task(d.run())


async def stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_wakes_when_settling_ends_not_at_the_next_poll(env):
    d = env.dispatcher(settle_seconds=0.2, poll_seconds=30)
    await approved(env.broker)
    steps, task = await run_counting_steps(d)
    try:
        await asyncio.sleep(0.05)
        assert len(steps) == 1 and env.host.calls == []  # still settling: host not contacted
        env.now[0] += 0.2
        for _ in range(40):
            if env.host.sent:
                break
            await asyncio.sleep(0.05)
        assert len(env.host.sent) == 1 and len(steps) == 2
    finally:
        await stop(task)


async def test_run_does_not_spin_on_settled_work_the_host_deferred(env):
    d = env.dispatcher(settle_seconds=0.2, poll_seconds=30)
    env.host.state = "busy"
    await approved(env.broker)
    steps, task = await run_counting_steps(d)
    try:
        await asyncio.sleep(0.05)
        env.now[0] += 0.2
        await asyncio.sleep(0.5)
        assert env.host.calls == ["confirm", "status"] and env.host.sent == []
        assert len(steps) == 2  # the deferred batch waits for the poll or a broker change
    finally:
        await stop(task)


async def test_revocation_racing_the_send_exposes_nothing(env):
    d = env.dispatcher()
    channel = await approved(env.broker)
    env.host.during_send = lambda: env.broker.revoke(channel)
    await d.step()
    assert "authority_ended_during_dispatch" in states(env.store)[0][1]
    assert (await env.broker.receive("zcode", after=0, timeout=0))["messages"] == []  # woken turn gets no body
    await d.step()
    assert states(env.store)[0][0] == "authority_ended"


async def test_busy_and_unknown_targets_are_deferred_not_interrupted(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.state = "busy"
    await d.step()
    await d.step()
    assert env.host.sent == [] and env.host.calls.count("status") == 1  # backoff, no hammering
    env.now[0] += 11
    env.host.state = "unknown"
    await d.step()
    assert env.host.sent == []
    codes = {i["code"] for i in env.broker.snapshot("zcode")["incidents"]}
    assert {"wake_deferred_busy", "wake_deferred_unknown"} <= codes
    env.now[0] += 11
    env.host.state = "idle"
    await d.step()
    assert len(env.host.sent) == 1


async def test_already_waiting_receiver_needs_no_wake(env):
    d = env.dispatcher()
    waiting = asyncio.create_task(env.broker.receive("zcode", after=0, timeout=5))
    await asyncio.sleep(0)
    await approved(env.broker)
    assert (await waiting)["messages"]
    await d.step()
    assert env.host.sent == [] and env.host.calls == []


async def test_timeout_is_ambiguous_and_unknown_outcome_is_never_retried(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.send_mode = "hang"
    await d.step()
    assert states(env.store)[0][0] == "sent_unconfirmed"
    env.host.send_mode = "ack"
    for _ in range(3):
        await d.step()
    assert len(env.host.sent) == 1 and "reconcile" in env.host.calls
    env.now[0] += 61
    await d.step()
    assert states(env.store)[0][0] == "unconfirmed_abandoned" and len(env.host.sent) == 1
    assert "wake_unconfirmed_abandoned" in {i["code"] for i in env.broker.snapshot("zcode")["incidents"]}


async def test_reconciliation_drives_retry_with_the_same_dispatch_id(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.send_mode = "malformed"
    await d.step()
    env.host.send_mode, env.host.reconcile_result = "ack", "not_delivered"
    await d.step()
    assert [s[0] for s in env.host.sent] == [env.host.sent[0][0]] * 2  # idempotency key reused
    assert states(env.store)[0][0] == "acknowledged"


async def test_reconciled_as_delivered_is_not_resent(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.send_mode, env.host.reconcile_result = "malformed", "delivered"
    await d.step()
    await d.step()
    assert len(env.host.sent) == 1 and states(env.store)[0][0] == "acknowledged"


async def test_restart_with_persisted_intent_is_treated_as_ambiguous(env):
    channel = await approved(env.broker)
    message = env.broker.db.execute("SELECT id, seq FROM messages").fetchone()
    # Simulated crash: intent was persisted, the process died before the send outcome was known.
    env.store.create("zcode", TARGET, [message["id"]], message["seq"] - 1, env.now[0])
    d = env.dispatcher()
    await d.step()
    assert env.host.sent == [] and states(env.store)[0][0] == "intent"  # unknown: no blind resend
    got = await env.broker.receive("zcode", after=0, timeout=0)  # the turn did start before the crash
    await env.broker.send("zcode", channel, "answer", "42", "a1", got["messages"][0]["id"])
    await d.step()
    assert states(env.store)[0][0] == "replied" and env.host.sent == []


async def test_acknowledged_without_a_turn_is_reported_not_called_success(env):
    d = env.dispatcher()
    await approved(env.broker)
    await d.step()
    env.now[0] += 61
    await d.step()
    assert states(env.store)[0][0] == "no_turn_observed"
    assert "wake_no_turn_observed" in {i["code"] for i in env.broker.snapshot("zcode")["incidents"]}


async def test_stale_target_suspends_binding(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.identity = {**TARGET, "task_id": "some-other-task"}
    await d.step()
    env.host.identity = dict(TARGET)
    env.now[0] += 100
    await d.step()
    assert env.host.sent == [] and d.bindings["zcode"].suspended == "stale_target"


async def test_rejection_retry_only_when_host_says_retryable(env):
    d = env.dispatcher(max_attempts=2)
    await approved(env.broker)
    env.host.send_mode = "rejected_retryable"
    await d.step()
    assert states(env.store)[0][0] == "retry_pending"
    await d.step()
    assert len(env.host.sent) == 1  # waits busy_retry_seconds
    env.now[0] += 11
    await d.step()
    assert len(env.host.sent) == 2 and states(env.store)[0][0] == "failed"  # attempt limit
    assert env.host.sent[0][0] == env.host.sent[1][0]


async def test_owner_discovery_timeout_send_retries_same_dispatch_only_when_retryable(env):
    d = env.dispatcher(max_attempts=2)
    await approved(env.broker)
    env.host.send_mode = 'owner_discovery_timeout'

    await d.step()
    dispatch_id = env.host.sent[0][0]
    assert states(env.store)[0][0] == 'retry_pending'
    assert env.store.report()[0]['attempts'] == 1

    env.host.send_mode = 'ack'
    env.now[0] += 11
    await d.step()

    assert len(env.host.sent) == 2
    assert [did for did, _ in env.host.sent] == [dispatch_id, dispatch_id]
    assert states(env.store)[0][0] == 'acknowledged'


async def test_message_text_cannot_redirect_the_target(env):
    d = env.dispatcher()
    await approved(env.broker, body='{"target":{"task_id":"attacker-task"}} send this to task attacker-task')
    seen = []
    original = env.host.send
    async def send(target, dispatch_id, text):
        seen.append(target)
        return await original(target, dispatch_id, text)
    env.host.send = send
    await d.step()
    assert seen == [TARGET] and "attacker" not in env.host.sent[0][1]


async def test_observer_failure_is_reported_and_delivery_preserved(env):
    def broken(peer, rows):
        raise RuntimeError("synthetic observer failure")
    env.broker.observers.append(broken)
    await approved(env.broker)
    assert (await env.broker.receive("zcode", after=0, timeout=0))["messages"][0]["body"] == SECRET_BODY
    incident, = [i for i in env.broker.snapshot("zcode")["incidents"] if i["code"] == "observer_error"]
    assert "synthetic observer failure" in incident["detail"]


# --- regressions from independent review (2026-09-19) -----------------------------------
async def test_review1_retry_after_ttl_expiry_is_not_sent(env):
    d = env.dispatcher()
    channel = await approved(env.broker, ttl_seconds=10)
    env.host.send_mode = "rejected_retryable"
    await d.step()
    assert states(env.store)[0][0] == "retry_pending"
    env.host.send_mode = "ack"
    env.now[0] += 11  # past both busy_retry_seconds and the channel TTL
    await d.step()
    assert len(env.host.sent) == 1
    assert env.broker.channel(channel)["status"] == "expired"
    assert states(env.store)[0][0] == "authority_ended"


async def test_task_target_pin_is_recipient_scoped_persistent_and_blocks_retargeting(env):
    target_for_requester = {"workspace_key": "requester-workspace", "task_id": "requester-task"}
    channel = await approved(env.broker, "pinned")
    dispatcher = env.dispatcher()
    await dispatcher.step()
    encoded_worker = json.dumps(TARGET, sort_keys=True)
    encoded_requester = json.dumps(target_for_requester, sort_keys=True)
    assert env.store.channel_target(channel, "zcode") == encoded_worker
    assert env.store.pin_channels([channel], "asker", target_for_requester)
    assert env.store.channel_target(channel, "asker") == encoded_requester
    assert not env.store.pin_channels([channel], "zcode", target_for_requester)

    initial = (await env.broker.receive("zcode", channel, timeout=0))["messages"][0]
    await env.broker.send("zcode", channel, "answer", "finished", "finished", initial["id"])
    await dispatcher.step()
    assert env.store.report()[0]["state"] == "replied"
    sends_before_restart = len(env.host.sent)
    restarted = env.reopen()
    assert env.store.channel_target(channel, "zcode") == encoded_worker
    assert env.store.channel_target(channel, "asker") == encoded_requester

    replacement = {"workspace_key": "replacement", "task_id": "replacement"}
    restarted.bindings["zcode"].target = replacement
    env.host.calls.clear()
    env.host.identity = dict(replacement)
    await env.broker.continue_channel("asker", channel, "same task, next round", "next-round")
    await restarted.step()
    assert env.host.calls == []
    assert len(env.host.sent) == sends_before_restart
    assert env.store.channel_target(channel, "zcode") == encoded_worker


async def test_review2_retry_rechecks_target_and_busy_state(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.send_mode = "rejected_retryable"
    await d.step()
    env.host.send_mode, env.host.state = "ack", "busy"
    env.now[0] += 11
    await d.step()
    assert len(env.host.sent) == 1 and states(env.store)[0][0] == "retry_pending"
    env.host.state, env.host.identity = "idle", {**TARGET, "task_id": "some-other-task"}
    env.now[0] += 11
    await d.step()
    assert len(env.host.sent) == 1 and states(env.store)[0][0] == "stale_target"


async def test_review2_retry_proceeds_once_target_is_idle_again(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.send_mode = "rejected_retryable"
    await d.step()
    env.host.send_mode, env.host.state = "ack", "busy"
    env.now[0] += 11
    await d.step()
    env.host.state = "idle"
    env.now[0] += 11
    await d.step()
    assert len(env.host.sent) == 2 and env.host.sent[0][0] == env.host.sent[1][0]
    assert states(env.store)[0][0] == "acknowledged"


@pytest.mark.parametrize("hook", ["during_reconcile", "during_confirm"])
async def test_review3_revocation_during_async_checks_blocks_the_resend(env, hook):
    d = env.dispatcher()
    channel = await approved(env.broker)
    env.host.send_mode = "malformed"
    await d.step()
    assert states(env.store)[0][0] == "sent_unconfirmed"
    env.host.send_mode, env.host.reconcile_result = "ack", "not_delivered"
    if hook == "during_reconcile":
        env.host.during_reconcile = lambda: env.broker.revoke(channel)
    else:  # revoked later still: while the pre-send target confirmation is awaited
        original = env.host.confirm
        async def confirm(target):
            await env.broker.revoke(channel)
            return await original(target)
        env.host.confirm = confirm
    await d.step()
    assert len(env.host.sent) == 1
    await d.step()
    assert len(env.host.sent) == 1 and states(env.store)[0][0] == "authority_ended"


async def test_review4_receiving_a_newer_channel_does_not_hide_an_older_unread_question(env):
    d = env.dispatcher()
    older = await approved(env.broker, "k1", body="older question")
    newer = await approved(env.broker, "k2", body="newer question")
    got = await env.broker.receive("zcode", newer, after=0, timeout=0)
    assert [m["body"] for m in got["messages"]] == ["newer question"]
    await d.step()
    (dispatch_id, _), = env.host.sent
    older_id = env.broker.db.execute("SELECT id FROM messages WHERE channel=?", (older,)).fetchone()[0]
    assert env.store.messages(dispatch_id) == [older_id]


async def test_review5_late_acknowledgment_does_not_overwrite_tool_executed(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.during_send = lambda: env.broker.receive("zcode", after=0, timeout=0)
    await d.step()
    state, events = states(env.store)[0]
    assert state == "tool_executed"
    assert events.index("tool_executed") < events.index("late_acknowledged")


async def test_review5_late_ambiguous_failure_does_not_overwrite_tool_executed(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.during_send = lambda: env.broker.receive("zcode", after=0, timeout=0)
    env.host.send_mode = "malformed"
    await d.step()
    await d.step()
    assert states(env.store)[0][0] == "tool_executed" and len(env.host.sent) == 1


async def test_retrieval_observed_while_retry_gate_is_awaited_cancels_the_resend(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.send_mode = "rejected_retryable"
    await d.step()
    env.host.send_mode = "ack"
    original = env.host.status
    async def status(target):
        await env.broker.receive("zcode", after=0, timeout=0)  # the session picks the message up by itself
        return await original(target)
    env.host.status = status
    env.now[0] += 11
    await d.step()
    assert len(env.host.sent) == 1 and states(env.store)[0][0] == "tool_executed"


def test_unknown_binding_keys_are_rejected(tmp_path):
    write_config(tmp_path, {"enabled": True, "bindings": {"zcode": {"adapter": "command", "command": ["bridge"], "target": TARGET, "timeout_seconds": 5}}})
    with pytest.raises(WakeConfigError):
        load_config(tmp_path, {"zcode"})


@pytest.mark.parametrize("timeout", ["invalid", True, 0, -5, 100000, None, [30]])
def test_review6_binding_timeout_is_validated(tmp_path, timeout):
    write_config(tmp_path, {"enabled": True, "bindings": {"zcode": {"adapter": "command", "command": ["bridge"], "target": TARGET, "timeout": timeout}}})
    with pytest.raises(WakeConfigError):
        load_config(tmp_path, {"zcode"})


async def test_review6_invalid_timeout_disables_adapter_and_server_still_serves(tmp_path):
    write_config(tmp_path, {"enabled": True, "bindings": {"zcode": {"adapter": "command", "command": ["bridge"], "target": TARGET, "timeout": "invalid"}}})
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            channel, zcode = await round_trip(http)
            got = (await http.post("/call", headers=zcode, json={"action": "receive", "channel": channel, "timeout": 0})).json()
            assert got["messages"][0]["body"] == "2+2?"
        assert getattr(app.state, "wakeup", None) is None
        assert "wakeup_config_error" in {i["code"] for i in app.state.broker.snapshot()["incidents"]}


# --- regressions from independent review round 2: cancelled resend, then restart ----------
async def first_send_then_cancelled_resend(env, variant, **options):
    """Steps 1-2 of the review scenario. Returns with two recorded sends and a cancelled step."""
    d = env.dispatcher(**options)
    await approved(env.broker)
    if variant == "rejected_retryable":
        env.host.send_mode = "rejected_retryable"
        await d.step()
        assert states(env.store)[0][0] == "retry_pending"
        env.now[0] += 11
    else:  # ambiguous outcome, then reconciliation says not delivered
        env.host.send_mode = "malformed"
        await d.step()
        assert states(env.store)[0][0] == "sent_unconfirmed"
        env.host.reconcile_result = "not_delivered"
    env.host.send_mode = "cancel_after_effect"
    with pytest.raises(asyncio.CancelledError):
        await d.step()
    assert len(env.host.sent) == 2


@pytest.mark.parametrize("variant", ["rejected_retryable", "ambiguous_then_not_delivered"])
async def test_review7_cancelled_resend_is_reconciled_after_restart_not_blindly_resent(env, variant):
    await first_send_then_cancelled_resend(env, variant)
    d = env.reopen()
    env.host.send_mode, env.host.reconcile_result = "ack", "unknown"
    env.host.calls.clear()
    env.now[0] += 11
    await d.step()
    assert len(env.host.sent) == 2  # the reviewer's failing assertion: was 3
    assert env.host.calls == ["reconcile"]  # reconciliation, and no confirm/status/send after it
    assert env.store.report()[0]["attempts"] == 2  # attempt accounting survived the restart
    env.now[0] += 61
    await d.step()
    assert len(env.host.sent) == 2 and states(env.store)[0][0] == "unconfirmed_abandoned"


@pytest.mark.parametrize("variant", ["rejected_retryable", "ambiguous_then_not_delivered"])
async def test_review7_confirmed_non_delivery_still_retries_within_the_attempt_limit(env, variant):
    await first_send_then_cancelled_resend(env, variant, max_attempts=3)
    d = env.reopen(max_attempts=3)
    env.host.send_mode, env.host.reconcile_result = "ack", "not_delivered"
    env.now[0] += 11
    await d.step()
    assert len(env.host.sent) == 3 and len({s[0] for s in env.host.sent}) == 1  # same dispatch id
    assert states(env.store)[0][0] == "acknowledged" and env.store.report()[0]["attempts"] == 3


@pytest.mark.parametrize("variant", ["rejected_retryable", "ambiguous_then_not_delivered"])
async def test_review7_attempt_limit_counts_the_cancelled_attempt(env, variant):
    await first_send_then_cancelled_resend(env, variant, max_attempts=2)
    d = env.reopen(max_attempts=2)
    env.host.send_mode, env.host.reconcile_result = "ack", "not_delivered"
    env.now[0] += 11
    await d.step()
    assert len(env.host.sent) == 2 and states(env.store)[0][0] == "failed"


async def test_review7_retrieval_during_cancelled_resend_survives_restart(env):
    env.host.during_send = None
    d = env.dispatcher()
    await approved(env.broker)
    env.host.send_mode = "rejected_retryable"
    await d.step()
    env.now[0] += 11
    env.host.send_mode = "cancel_after_effect"
    env.host.during_send = lambda: env.broker.receive("zcode", after=0, timeout=0)
    with pytest.raises(asyncio.CancelledError):
        await d.step()
    d = env.reopen()
    env.host.send_mode, env.host.during_send = "ack", None
    env.host.calls.clear()
    env.now[0] += 11
    await d.step()
    assert states(env.store)[0][0] == "tool_executed"  # stronger observed stage kept
    assert len(env.host.sent) == 2 and env.host.calls == []  # nothing to reconcile or resend


async def test_review7_unexpected_adapter_exception_during_resend_is_also_reconciled(env):
    d = env.dispatcher()
    await approved(env.broker)
    env.host.send_mode = "rejected_retryable"
    await d.step()
    env.now[0] += 11
    async def explode():
        raise RuntimeError("synthetic adapter bug after the side effect")
    env.host.during_send, env.host.send_mode = explode, "ack"
    with pytest.raises(RuntimeError):
        await d.step()
    env.host.during_send = None
    env.host.calls.clear()
    env.now[0] += 11
    await d.step()  # same process, no restart
    assert len(env.host.sent) == 2 and env.host.calls == ["reconcile"]
    assert states(env.store)[0][0] == "intent"


async def test_review7_every_send_is_preceded_by_a_persisted_in_flight_state(env):
    d = env.dispatcher()
    await approved(env.broker)
    seen = []
    env.host.during_send = lambda: asyncio.sleep(0, seen.append((env.store.report()[0]["state"], env.store.report()[0]["attempts"])))
    env.host.send_mode = "rejected_retryable"
    await d.step()
    env.now[0] += 11
    env.host.send_mode = "ack"
    await d.step()
    assert seen == [("intent", 1), ("intent", 2)]  # durable before each side effect, retries included


# --- command adapter -------------------------------------------------------------------
BRIDGE = '''
import json, sys, time
request = json.load(sys.stdin)
mode = sys.argv[1]
with open(sys.argv[2], "a") as log:
    log.write(json.dumps(request) + "\\n")
if mode == "hang": time.sleep(30)
if mode == "garbage": print("not json"); sys.exit(0)
if mode == "reject": print(json.dumps({"ok": False, "error": "rejected", "retryable": True})); sys.exit(0)
reply = {"confirm": {"target": request.get("target")}, "status": {"state": "idle"},
         "send": {"accepted": True, "host_ref": "r1"}, "reconcile": {"result": "delivered"}}[request["op"]]
print(json.dumps({"ok": True, **reply}))
'''


@pytest.fixture
def bridge(tmp_path):
    script = tmp_path / "bridge.py"
    script.write_text(BRIDGE)
    return lambda mode, timeout=10: CommandAdapter([sys.executable, str(script), mode, str(tmp_path / "bridge.log")], timeout)


async def test_command_adapter_contract(bridge, tmp_path):
    ok = bridge("ok")
    assert await ok.confirm(TARGET) == TARGET and await ok.status(TARGET) == "idle"
    assert await ok.send(TARGET, "d1", "wake") == {"host_ref": "r1"}
    assert await ok.reconcile(TARGET, "d1") == "delivered"
    with pytest.raises(AdapterError) as error:
        await bridge("garbage").send(TARGET, "d1", "wake")
    assert error.value.kind == "ambiguous"  # a lost send reply is never read as "not sent"
    with pytest.raises(AdapterError) as error:
        await bridge("garbage").status(TARGET)
    assert error.value.kind == "malformed"
    with pytest.raises(AdapterError) as error:
        await bridge("hang", timeout=0.5).send(TARGET, "d1", "wake")
    assert error.value.kind == "ambiguous"
    with pytest.raises(AdapterError) as error:
        await bridge("reject").send(TARGET, "d1", "wake")
    assert error.value.kind == "rejected" and error.value.retryable
    with pytest.raises(AdapterError) as error:
        await CommandAdapter([str(tmp_path / "missing-binary")]).status(TARGET)
    assert error.value.kind == "unavailable"


async def test_command_adapter_preserves_owner_discovery_timeout_kind_and_retryability(tmp_path):
    script = tmp_path / 'owner-timeout-bridge.py'
    script.write_text(
        'import json\n'
        'print(json.dumps({"ok": False, "error": "owner_discovery_timeout", '
        '"detail": "owner discovery timed out", "retryable": True}))\n')
    adapter = CommandAdapter([sys.executable, str(script)])

    with pytest.raises(AdapterError) as error:
        await adapter.confirm(TARGET)

    assert error.value.kind == 'owner_discovery_timeout'
    assert error.value.retryable is True


# --- configuration and server integration ---------------------------------------------------
def write_config(path, value, mode=0o600):
    target = path / "wakeup.json"
    target.write_text(json.dumps(value))
    os.chmod(target, mode)


def test_config_is_explicit_and_disabled_by_default(tmp_path):
    binding = {"adapter": "command", "command": ["bridge"], "target": TARGET}
    write_config(tmp_path, {"bindings": {"zcode": binding}})
    assert load_config(tmp_path, {"zcode"})[1] is None  # "enabled": true is required
    write_config(tmp_path, {"enabled": True, "bindings": {"zcode": binding}, "turn_timeout": 30})
    bindings, options = load_config(tmp_path, {"zcode"})
    assert bindings["zcode"].target == TARGET and options == {"turn_timeout": 30}
    for bad in ({"enabled": True, "bindings": {"ghost": binding}},
                {"enabled": True, "bindings": {"zcode": {**binding, "target": {}}}},
                {"enabled": True, "bindings": {"zcode": {"adapter": "command", "command": ["bridge"]}}},
                {"enabled": True, "bindings": {"zcode": {**binding, "command": "sh -c x"}}},
                {"enabled": True, "bindings": {"zcode": binding}, "max_attempts": 99}):
        write_config(tmp_path, bad)
        with pytest.raises(WakeConfigError):
            load_config(tmp_path, {"zcode"})
    if sys.platform != "win32":  # Windows access is checked through ACLs in test_os_adapter.py
        write_config(tmp_path, {"enabled": True, "bindings": {"zcode": binding}}, mode=0o666)
        with pytest.raises(WakeConfigError):
            load_config(tmp_path, {"zcode"})


def make_app(tmp_path):
    (tmp_path / "server.json").write_text(json.dumps({"admin_token": "admin-secret", "peers": {"asker": "asker-secret", "zcode": "zcode-secret"}}))
    for peer in ("asker", "zcode"):
        (tmp_path / f"{peer}.json").write_text(json.dumps({"client": "generic"}))
    return create_app(tmp_path)


async def round_trip(http):
    asker, zcode, admin = ({"Authorization": f"Bearer {name}-secret"} for name in ("asker", "zcode", "admin"))
    channel = (await http.post("/call", headers=asker, json={"action": "open", "target": "zcode", "body": "2+2?", "key": "k"})).json()["id"]
    assert (await http.post("/admin", headers=admin, json={"action": "approve", "channel": channel})).status_code == 200
    return channel, zcode


async def test_server_without_or_with_broken_adapter_still_serves(tmp_path):
    for broken in (False, True):
        data = tmp_path / str(broken)
        data.mkdir()
        if broken:
            write_config(data, {"enabled": True, "bindings": {"ghost": {}}})
        app = make_app(data)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
                channel, zcode = await round_trip(http)
                got = (await http.post("/call", headers=zcode, json={"action": "receive", "channel": channel, "timeout": 0})).json()
                assert got["messages"][0]["body"] == "2+2?"
            assert getattr(app.state, "wakeup", None) is None
            codes = {i["code"] for i in app.state.broker.snapshot()["incidents"]}
            assert ("wakeup_config_error" in codes) == broken
        assert not (data / "wakeup.sqlite3").exists()


async def test_server_with_command_adapter_dispatches_and_stops_cleanly(tmp_path, bridge):
    adapter = bridge("ok")
    write_config(tmp_path, {"enabled": True, "settle_seconds": 0, "poll_seconds": 0.05,
                            "bindings": {"zcode": {"adapter": "command", "command": adapter.command, "target": TARGET}}})
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            channel, zcode = await round_trip(http)
            for _ in range(100):
                report = app.state.wakeup.dispatcher.store.report()
                if report and report[0]["state"] == "acknowledged":
                    break
                await asyncio.sleep(0.05)
            assert report[0]["state"] == "acknowledged"
            got = (await http.post("/call", headers=zcode, json={"action": "receive", "channel": channel, "timeout": 0})).json()
            assert got["messages"][0]["body"] == "2+2?"
            assert app.state.wakeup.dispatcher.store.report()[0]["state"] == "tool_executed"
        task = app.state.wakeup.task
    assert task.done()
    requests = [json.loads(line) for line in (tmp_path / "bridge.log").read_text().splitlines()]
    assert [r["op"] for r in requests] == ["confirm", "status", "send"]
    assert all(r["target"] == TARGET for r in requests) and "2+2?" not in json.dumps(requests)


@pytest.fixture
def recovery(env):
    d = env.dispatcher(restore_timeout_seconds=20)
    d.bindings['zcode'].restore_enabled = True
    env.cold, env.restore_calls, env.during_restore, env.during_confirm = 'no_owner', [], None, None
    original_confirm = env.host.confirm

    async def confirm(target):
        if env.during_confirm:
            await env.during_confirm()
        if env.cold:
            raise AdapterError(env.cold, 'synthetic cold target')
        return await original_confirm(target)

    async def restore(target):
        env.restore_calls.append(dict(target))
        assert env.store.channel_target(env.channel, 'zcode') == json.dumps(target, sort_keys=True)
        assert env.store.db.execute('SELECT state FROM recoveries').fetchone()[0] == 'requested'
        if env.during_restore:
            await env.during_restore()
        else:
            env.cold = None

    env.host.confirm, env.host.restore = confirm, restore
    env.d = d
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize('absence', ['no_owner', 'socket_missing', 'startup', 'owner_discovery_timeout'])
async def test_cold_pending_batch_restores_once_and_sends_once(recovery, absence):
    e = recovery
    e.cold, e.channel = absence, await approved(e.broker)
    await approved(e.broker, 'second')
    await e.d.step()
    await e.d.step()
    assert e.restore_calls == [TARGET]
    assert len(e.host.sent) == 1
    assert e.host.calls == ['confirm', 'status']
    assert len(e.store.messages(e.store.report()[0]['id'])) == 2
    assert e.store.db.execute('SELECT state FROM recoveries').fetchone()[0] == 'ready'


@pytest.mark.asyncio
async def test_owner_discovery_timeout_recovery_rechecks_exact_identity_after_navigation(recovery):
    e = recovery
    e.cold, e.channel = 'owner_discovery_timeout', await approved(e.broker)

    async def opened_wrong_target():
        e.cold = None
        e.host.identity = {**TARGET, 'task_id': 'different-task'}

    e.during_restore = opened_wrong_target
    await e.d.step()

    assert e.restore_calls == [TARGET]
    assert e.host.calls == ['confirm']
    assert e.host.sent == []
    assert e.d.bindings['zcode'].suspended == 'stale_target'


@pytest.mark.asyncio
async def test_hot_idle_does_not_restore_with_opt_in(recovery):
    e = recovery
    e.cold, e.channel = None, await approved(e.broker)
    await e.d.step()
    assert e.restore_calls == [] and len(e.host.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['permissions', 'protocol', 'invalid_socket', 'unavailable', 'stale_target'])
async def test_incompatible_or_unverified_failure_never_restores(recovery, failure):
    e = recovery
    e.cold, e.channel = failure, await approved(e.broker)
    await e.d.step()
    assert e.restore_calls == [] and e.host.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('absence', ['no_owner', 'owner_discovery_timeout'])
async def test_cold_recovery_requires_explicit_binding_opt_in(recovery, absence):
    e = recovery
    e.d.bindings['zcode'].restore_enabled = False
    e.cold = absence
    e.channel = await approved(e.broker)
    await e.d.step()
    assert e.restore_calls == [] and e.host.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['busy', 'unknown'])
async def test_recovery_never_sends_unknown_even_when_generic_override_enabled(recovery, state):
    e = recovery
    e.channel = await approved(e.broker)
    e.host.state, e.d.send_when_unknown = state, True
    await e.d.step()
    assert e.restore_calls == [TARGET] and e.host.sent == []
    e.now[0] += 21
    await e.d.step()
    assert e.store.db.execute('SELECT state FROM recoveries').fetchone()[0] == 'timed_out'
    assert e.restore_calls == [TARGET] and e.host.sent == []


@pytest.mark.asyncio
async def test_recovery_episode_survives_restart_and_coalesces_new_messages(recovery):
    e = recovery
    e.channel = await approved(e.broker)

    async def remain_cold():
        pass
    e.during_restore = remain_cold
    await e.d.step()
    e.now[0] += 10
    await approved(e.broker, 'second')
    e.d = e.reopen(restore_timeout_seconds=20)
    e.d.bindings['zcode'].restore_enabled = True
    await e.d.step()
    assert e.restore_calls == [TARGET] and e.host.sent == []
    assert len(json.loads(e.store.db.execute('SELECT message_ids FROM recoveries').fetchone()[0])) == 2
    e.now[0] += 11
    await e.d.step()
    assert e.store.db.execute('SELECT state FROM recoveries').fetchone()[0] == 'timed_out'
    e.d = e.reopen(restore_timeout_seconds=20)
    e.d.bindings['zcode'].restore_enabled = True
    await e.d.step()
    assert e.restore_calls == [TARGET] and e.host.sent == []
    # Owner readiness after the episode expired still permits the authorized send.
    e.cold, e.now[0] = None, e.now[0] + 10
    await e.d.step()
    assert len(e.host.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['confirm', 'restore'])
@pytest.mark.parametrize('authority_end', ['revoke', 'expiry'])
@pytest.mark.parametrize('cold_kind', ['no_owner', 'owner_discovery_timeout'])
async def test_authority_ending_during_preparation_blocks_send(recovery, stage, authority_end, cold_kind):
    e = recovery
    e.cold = cold_kind
    e.channel = await approved(e.broker, ttl_seconds=30)

    async def end_authority():
        if authority_end == 'revoke':
            await e.broker.revoke(e.channel)
        else:
            e.now[0] += 31
        e.cold = None
    if stage == 'confirm':
        e.during_confirm = end_authority
    else:
        e.during_restore = end_authority
    await e.d.step()
    assert e.host.sent == []
    assert len(e.restore_calls) == (1 if stage == 'restore' else 0)


@pytest.mark.asyncio
async def test_binding_change_while_restoring_blocks_send_and_retargeting(recovery):
    e = recovery
    e.channel = await approved(e.broker)

    async def drift():
        e.d.bindings['zcode'].target['task_id'] = 'replacement'
        e.cold = None
    e.during_restore = drift
    await e.d.step()
    assert e.host.sent == [] and e.restore_calls == [TARGET]
    assert e.d.bindings['zcode'].suspended == 'stale_target'


@pytest.mark.asyncio
async def test_ambiguous_send_after_restore_never_restores_or_resends_on_restart(recovery):
    e = recovery
    e.channel = await approved(e.broker)
    e.host.send_mode = 'malformed'
    await e.d.step()
    assert e.store.report()[0]['state'] == 'sent_unconfirmed'
    e.cold = 'no_owner'
    e.d = e.reopen(restore_timeout_seconds=20)
    e.d.bindings['zcode'].restore_enabled = True
    e.now[0] += 61
    await e.d.step()
    assert e.restore_calls == [TARGET] and len(e.host.sent) == 1
    assert e.store.report()[0]['state'] == 'unconfirmed_abandoned'


@pytest.mark.parametrize('value', [True, False, 'yes', 1])
def test_restore_binding_configuration_is_explicit_boolean(tmp_path, value):
    config = {'enabled': True, 'bindings': {'zcode': {
        'adapter': 'command', 'command': [sys.executable, 'bridge.py'], 'target': TARGET, 'restore': value,
    }}}
    (tmp_path / 'wakeup.json').write_text(json.dumps(config))
    if isinstance(value, bool):
        bindings, _ = load_config(tmp_path, ['zcode'])
        assert bindings['zcode'].restore_enabled is value
    else:
        with pytest.raises(WakeConfigError, match='restore must be boolean'):
            load_config(tmp_path, ['zcode'])


@pytest.mark.asyncio
async def test_cancelled_restore_keeps_episode_and_does_not_navigate_again(recovery):
    e = recovery
    e.channel = await approved(e.broker)

    async def cancelled():
        raise asyncio.CancelledError()
    e.during_restore = cancelled
    with pytest.raises(asyncio.CancelledError):
        await e.d.step()
    assert e.store.report() == []
    e.d = e.reopen(restore_timeout_seconds=20)
    e.d.bindings['zcode'].restore_enabled = True
    await e.d.step()
    assert e.restore_calls == [TARGET] and e.host.sent == []


@pytest.mark.asyncio
async def test_new_same_target_work_joins_pending_episode_after_original_revocation(recovery):
    e = recovery
    e.channel = await approved(e.broker)

    async def new_work():
        await e.broker.revoke(e.channel)
        await approved(e.broker, 'second')
    e.during_restore = new_work
    await e.d.step()
    e.now[0] += 10
    await e.d.step()
    assert e.restore_calls == [TARGET] and e.host.sent == []
    assert len(json.loads(e.store.db.execute('SELECT message_ids FROM recoveries').fetchone()[0])) == 2
