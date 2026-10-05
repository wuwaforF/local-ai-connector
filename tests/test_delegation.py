import asyncio
from types import SimpleNamespace

import pytest

from local_ai_connector.core import Broker, ConnectorError
from local_ai_connector.delegation import delegate


class Context:
    def __init__(self, action="accept"):
        self.protocol_version = "2025-11-25"
        self.input_responses = None
        self.request_state = None
        self.client_capabilities = SimpleNamespace(elicitation=SimpleNamespace(form={}, url=None))
        self.session = SimpleNamespace(can_send_request=True, elicit_form=self.elicit_form, send_request=self.send_request)
        self.request_context = SimpleNamespace(meta=None)
        self.request_id = "test-request"
        self.confirmation_meta = None
        self.action = action
        self.prompts, self.progress = [], []
        self.on_elicit = None

    async def send_request(self, request, result_type, *, metadata):
        return await self.session.elicit_form(message=request.params.message,
            requested_schema=request.params.requested_schema, related_request_id=metadata.related_request_id)

    async def elicit(self, message, schema):
        self.prompts.append(message)
        if self.on_elicit:
            await self.on_elicit()
        return SimpleNamespace(action=self.action, data={} if self.action == "accept" else None)

    async def elicit_form(self, *, message, requested_schema, related_request_id):
        from mcp_types import ElicitResult
        r = await self.elicit(message, requested_schema)
        return ElicitResult(action=r.action, content=r.data, _meta=self.confirmation_meta)

    async def report_progress(self, progress, total=None, message=None):
        self.progress.append(message)


@pytest.fixture
def broker(tmp_path):
    b = Broker(tmp_path / "state.sqlite3")
    b.register("requester", "a")
    b.register("worker", "b")
    yield b
    b.close()


def operations(broker):
    async def call(ctx, **p):
        op = p.pop("action")
        if op == "open":
            return await broker.open("requester", p["target"], p["body"], p["key"], p["ttl_seconds"], p["idle_seconds"])
        if op == "result":
            return broker.delegation_result("requester", p["channel"])
        if op == "receive":
            return await broker.receive("requester", p["channel"], p["after"], p["timeout"])
        if op == "finish":
            return await broker.finish("requester", p["channel"])
        if op == "send":
            return await broker.send("requester", p["channel"], p["kind"], p["body"], p["key"], p["reply_to"])
        if op == "continue":
            return await broker.continue_channel("requester", p["channel"], p["body"], p["key"])
        if op == "round_result":
            return broker.round_result("requester", p["channel"], p["question_id"])
        raise AssertionError(op)

    async def decide(ctx, channel, decision):
        return await broker.decide(channel, decision == "accept", host_approval=("requester", decision))
    return call, decide


async def worker_reply(broker, text="real worker output"):
    question = (await broker.receive("worker", timeout=5))["messages"][0]
    await broker.send("worker", question["channel"], "answer", text, "answer", question["id"])


async def test_approval_then_reply_in_one_call_and_completed_replay(broker):
    ctx = Context()
    call, decide = operations(broker)
    async def before_approval():
        assert (await broker.receive("worker", timeout=0))["messages"] == []
    ctx.on_elicit = before_approval
    worker = asyncio.create_task(worker_reply(broker))
    result = await delegate(ctx, call, decide, "worker", "private task", "one", timeout_seconds=2)
    await worker
    assert result["state"] == "completed" and result["answer"]["body"] == "real worker output"
    assert "private task" in ctx.prompts[0] and "worker" in ctx.prompts[0]
    assert broker.channel(result["channel"])["idle_since"] is not None
    with broker.db:
        broker.db.execute("UPDATE channels SET status='closed' WHERE id=?", (result["channel"],))
    repeated = await delegate(ctx, call, decide, "worker", "private task", "one", timeout_seconds=2)
    assert repeated == result and len(ctx.prompts) == 1
    assert broker.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_rejected_confirmation_never_delivers(broker, action):
    ctx = Context(action)
    result = await delegate(ctx, *operations(broker), "worker", "private task", "no", timeout_seconds=1)
    assert result["state"] == "denied" and result["decision"] == action
    assert (await broker.receive("worker", timeout=0))["messages"] == []


@pytest.mark.parametrize("mode", ["absent", "url_only", "no_backchannel"])
async def test_unsupported_confirmation_creates_no_task(broker, mode):
    ctx = Context()
    if mode == "absent": ctx.client_capabilities = None
    elif mode == "url_only": ctx.client_capabilities.elicitation = SimpleNamespace(form=None, url={})
    else: ctx.session.can_send_request = False
    with pytest.raises(ConnectorError, match="聊天内确认"):
        await delegate(ctx, *operations(broker), "worker", "task", "unsupported")
    assert broker.snapshot("requester")["channels"] == []


async def test_approval_time_does_not_consume_execution_budget(broker):
    ctx = Context()
    async def delayed_approval(): await asyncio.sleep(0.06)
    ctx.on_elicit = delayed_approval
    worker = asyncio.create_task(worker_reply(broker))
    result = await delegate(ctx, *operations(broker), "worker", "task", "delay", timeout_seconds=1)
    await worker
    assert result["state"] == "completed"


async def test_timeout_resumes_existing_task_without_reapproval_or_resend(broker):
    ctx = Context()
    result = await delegate(ctx, *operations(broker), "worker", "task", "slow", timeout_seconds=1)
    assert result["state"] == "running" and result["request_key"] == "slow"
    await worker_reply(broker)
    completed = await delegate(ctx, *operations(broker), "worker", "task", "slow", timeout_seconds=1)
    assert completed["state"] == "completed" and len(ctx.prompts) == 1
    assert len(broker.snapshot()["channels"]) == 1


async def test_revocation_while_approval_is_open_does_not_deliver(broker):
    ctx = Context()
    async def revoke(): await broker.revoke(broker.snapshot()["channels"][0]["id"])
    ctx.on_elicit = revoke
    with pytest.raises(ConnectorError):
        await delegate(ctx, *operations(broker), "worker", "task", "revoked")
    assert (await broker.receive("worker", timeout=0))["messages"] == []


async def test_cancelled_tool_during_confirmation_leaves_no_delivery(broker):
    ctx = Context()
    entered = asyncio.Event()
    async def hold():
        entered.set()
        await asyncio.Future()
    ctx.on_elicit = hold
    job = asyncio.create_task(delegate(ctx, *operations(broker), "worker", "task", "cancelled"))
    await entered.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError): await job
    assert (await broker.receive("worker", timeout=0))["messages"] == []


async def test_pending_waits_are_internal_not_returned_to_model(broker):
    ctx = Context()
    call, decide = operations(broker)
    receives = 0
    async def delayed(ctx, **p):
        nonlocal receives
        if p["action"] == "receive":
            receives += 1
            if receives == 1: return {"state": "waiting", "cursor": 0, "messages": []}
            await worker_reply(broker)
        return await call(ctx, **p)
    result = await delegate(ctx, delayed, decide, "worker", "task", "waiting", timeout_seconds=1)
    assert result["state"] == "completed" and receives == 2


async def test_host_cancellation_metadata_is_not_discarded(broker):
    ctx = Context("cancel")
    ctx.confirmation_meta = {"reviewer": "host-review", "reason": "confirmation unavailable"}
    result = await delegate(ctx, *operations(broker), "worker", "task", "host-cancel")
    assert result["state"] == "denied"
    assert result["host_response_meta"] == ctx.confirmation_meta
    assert (await broker.receive("worker", timeout=0))["messages"] == []


async def test_confirmation_schema_uses_only_portable_root_fields(broker):
    from mcp_types import ElicitResult
    ctx = Context("cancel")
    async def strict_host(**params):
        schema = params["requested_schema"]
        assert schema == {"type": "object", "properties": {}}
        return ElicitResult(action="cancel")
    ctx.session.elicit_form = strict_host
    result = await delegate(ctx, *operations(broker), "worker", "task", "strict-schema")
    assert result["state"] == "denied"


@pytest.mark.parametrize("ending", ["denied", "revoked", "expired"])
async def test_host_permission_retry_cannot_revive_ended_task(broker, ending):
    from local_ai_connector.delegation import delegate_with_host_permission
    call, decide = operations(broker)
    ctx = Context()
    task = await broker.open("requester", "worker", "task", "host-retry")
    if ending == "denied":
        await broker.decide(task["id"], False)
    elif ending == "revoked":
        await broker.revoke(task["id"])
    else:
        broker.clock = lambda: task["expires"] + 1
    result = await delegate_with_host_permission(ctx, call, decide, "worker", "task", "host-retry")
    assert result["state"] == ending and result["next_tool"] is None
    assert broker.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert ctx.prompts == []

@pytest.mark.parametrize("expired", [False, True])
async def test_host_permission_closed_replay_returns_answer_only_within_authorization(broker, expired):
    from local_ai_connector.delegation import delegate_with_host_permission
    ctx = Context()
    call, decide = operations(broker)
    task = await delegate_with_host_permission(ctx, call, decide, "worker", "task", "closed-host")
    await worker_reply(broker)
    await broker.finish("requester", task["channel"])
    row = broker.channel(task["channel"])
    broker.clock = lambda: row["expires"] + 1 if expired else row["idle_since"] + 121
    if expired:
        with broker.db:
            broker.db.execute("UPDATE channels SET status='closed' WHERE id=?", (task["channel"],))
    result = await delegate_with_host_permission(ctx, call, decide, "worker", "task", "closed-host")
    assert result["next_tool"] is None
    if expired:
        assert result["state"] == "expired" and "answer" not in result
    else:
        assert result["state"] == "closed" and result["answer"]["body"] == "real worker output"
