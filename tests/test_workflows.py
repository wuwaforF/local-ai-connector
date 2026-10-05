import asyncio
import hashlib
import json
import sqlite3

import pytest

from local_ai_connector.core import Broker, ConnectorError
from local_ai_connector.wakeup import Binding, Dispatcher, WakeStore


OWNER, BUILDER, REVIEWER = "owner", "builder", "reviewer"
REGISTRATIONS = {
    BUILDER: {"provider": "codex_ingress", "session_id": "11111111-1111-4111-8111-111111111111", "cwd": "/builder"},
    REVIEWER: {"provider": "codex_ingress", "session_id": "22222222-2222-4222-8222-222222222222", "cwd": "/reviewer"},
}


@pytest.fixture
def broker(tmp_path):
    now = [100]
    value = Broker(tmp_path / "state.sqlite3", clock=lambda: now[0], conversations={})
    for peer in (OWNER, BUILDER, REVIEWER):
        value.register(peer, peer + "-token")
    value.test_clock = now
    yield value
    value.close()


def stage_payload(stage):
    return json.loads(stage["body"].rsplit("\n", 1)[1])


def build_result(stage, content="candidate", verification="checked"):
    payload = stage_payload(stage)
    return json.dumps({"schema_version": 1, "run_id": payload["run_id"], "stage_id": stage["id"],
                       "candidate_content": content, "verification": verification})


def review_result(stage, build, verdict="accept", findings="reviewed", **changes):
    payload = stage_payload(stage)
    output = {"schema_version": 1, "run_id": payload["run_id"], "stage_id": stage["id"],
              "build_stage_id": build["output"]["build_stage_id"],
              "candidate_sha256": build["output"]["candidate_sha256"],
              "verdict": verdict, "findings": findings}
    output.update(changes)
    return json.dumps(output)


async def answer_stage(broker, stage, body):
    cid = stage["channel"]
    await broker.decide(cid, True, host_approval=(OWNER, "accept"))
    question = (await broker.receive(stage["target"], cid, timeout=0))["messages"][0]
    return await broker.send(stage["target"], cid, "answer", body, "answer-" + stage["id"], question["id"])


async def start(broker, creation_key="creation-key", plan="Implement the requested change", **changes):
    return await broker.workflows.start(OWNER, plan, BUILDER, REVIEWER,
                                        creation_key, **changes)


@pytest.mark.asyncio
async def test_build_review_accept_requires_each_stage_approval_and_hashes_exact_candidate(broker):
    run = await start(broker)
    build = run["stages"][0]
    assert run["version"] == 1
    assert (await broker.receive(BUILDER, build["channel"], timeout=0))["messages"] == []
    await broker.decide(build["channel"], True, host_approval=(OWNER, "accept"))
    build_question = (await broker.receive(BUILDER, build["channel"], timeout=0))["messages"][0]
    assert build_question["body"] == build["body"]
    candidate = "exact candidate text"
    await broker.send(BUILDER, build["channel"], "answer", build_result(build, candidate), "build-answer", build_question["id"])

    run = await broker.workflows.resume(OWNER, run["id"], run["version"])
    review = run["stages"][1]
    assert run["state"] == "waiting" and review["role"] == "review"
    assert review["channel_observation"]["status"] == "pending"
    assert (await broker.receive(REVIEWER, review["channel"], timeout=0))["messages"] == []
    await answer_stage(broker, review, review_result(review, run["stages"][0]))
    run = await broker.workflows.resume(OWNER, run["id"], run["version"])

    assert run["state"] == "accepted"
    assert run["stages"][0]["output"]["candidate_sha256"] == hashlib.sha256(candidate.encode()).hexdigest()
    assert run["stages"][1]["output"]["candidate_sha256"] == run["stages"][0]["output"]["candidate_sha256"]


@pytest.mark.asyncio
async def test_revision_creates_fresh_approved_build_and_respects_limit(broker):
    run = await start(broker, max_revisions=1)
    build = run["stages"][0]
    await answer_stage(broker, build, build_result(build))
    run = await broker.workflows.resume(OWNER, run["id"], run["version"])
    review = run["stages"][1]
    await answer_stage(broker, review, review_result(review, run["stages"][0], "revise", "Fix edge case"))
    run = await broker.workflows.resume(OWNER, run["id"], run["version"])
    revised = run["stages"][2]
    assert revised["role"] == "build" and revised["channel_observation"]["status"] == "pending"
    assert (await broker.receive(BUILDER, revised["channel"], timeout=0))["messages"] == []

    await answer_stage(broker, revised, build_result(revised, "revised candidate"))
    run = await broker.workflows.resume(OWNER, run["id"], run["version"])
    review2 = run["stages"][3]
    await answer_stage(broker, review2, review_result(review2, run["stages"][2], "revise", "Still broken"))
    run = await broker.workflows.resume(OWNER, run["id"], run["version"])
    assert run["state"] == "revision_limit"
    assert len(run["stages"]) == 4


@pytest.mark.asyncio
async def test_zero_revision_limit_and_blocked_builder_are_terminal(broker):
    run = await start(broker, creation_key="zero", max_revisions=0)
    build = run["stages"][0]
    await answer_stage(broker, build, build_result(build))
    run = await broker.workflows.resume(OWNER, run["id"], run["version"])
    review = run["stages"][1]
    await answer_stage(broker, review, review_result(review, run["stages"][0], "revise"))
    run = await broker.workflows.resume(OWNER, run["id"], run["version"])
    assert run["state"] == "revision_limit" and len(run["stages"]) == 2

    other = await start(broker, creation_key="blocked")
    build = other["stages"][0]
    blocked = {"schema_version": 1, "run_id": other["id"], "stage_id": build["id"],
               "blocked": "missing input", "next_action": "supply the specification"}
    await answer_stage(broker, build, json.dumps(blocked))
    result = await broker.workflows.resume(OWNER, other["id"], other["version"])
    assert result["state"] == "worker_blocked" and len(result["stages"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "Here is the result: {}",
    '{"schema_version":1,"schema_version":1}',
    "[]",
])
async def test_non_object_narrative_and_duplicate_key_results_are_rejected(broker, body):
    run = await start(broker, creation_key="bad-" + str(len(body)))
    await answer_stage(broker, run["stages"][0], body)
    result = await broker.workflows.resume(OWNER, run["id"], run["version"])
    assert result["state"] == "invalid_result"
    assert result["stages"][0]["error"] == "workflow_result_json"
    assert len(result["stages"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"build_stage_id": "wrong-stage"}, {"candidate_sha256": "0" * 64}])
async def test_review_must_identify_exact_candidate_stage_and_hash(broker, changes):
    run = await start(broker)
    build = run["stages"][0]
    await answer_stage(broker, build, build_result(build))
    run = await broker.workflows.resume(OWNER, run["id"], run["version"])
    review = run["stages"][1]
    body = review_result(review, run["stages"][0], **changes)
    await answer_stage(broker, review, body)
    result = await broker.workflows.resume(OWNER, run["id"], run["version"])
    assert result["state"] == "invalid_result"
    assert result["stages"][1]["error"] == "workflow_candidate_mismatch"
    assert len(result["stages"]) == 2


async def test_concurrent_projection_and_lost_response_replay_create_one_successor(broker):
    run = await start(broker)
    await answer_stage(broker, run["stages"][0], build_result(run["stages"][0]))
    results = await asyncio.gather(*(broker.workflows.resume(OWNER, run["id"], run["version"]) for _ in range(2)),
                                   return_exceptions=True)
    assert sum(isinstance(r, ConnectorError) and r.code == "workflow_conflict" for r in results) == 1
    current = broker.workflows.status(OWNER, run["id"])
    assert len(current["stages"]) == 2
    assert broker.db.execute("SELECT count(*) FROM channels").fetchone()[0] == 2
    with pytest.raises(ConnectorError) as stale:
        await broker.workflows.resume(OWNER, run["id"], run["version"])
    assert stale.value.code == "workflow_conflict"
    assert broker.workflows.status(OWNER, run["id"])["version"] == current["version"]


async def test_restart_after_projection_recovers_intent_without_reaccepting_result(tmp_path):
    path = tmp_path / "restart.sqlite3"
    b = Broker(path)
    for peer in (OWNER, BUILDER, REVIEWER):
        b.register(peer, peer + "-token")
    try:
        run = await start(b)
        await answer_stage(b, run["stages"][0], build_result(run["stages"][0]))
        original_open = b.open
        async def fail_next_open(*args, **kwargs):
            raise RuntimeError("process stopped after projection")
        b.open = fail_next_open
        with pytest.raises(RuntimeError, match="after projection"):
            await b.workflows.resume(OWNER, run["id"], run["version"])
        pending = b.workflows.status(OWNER, run["id"])
        successor_id = pending["stages"][1]["id"]
        assert pending["stages"][1]["channel"] is None
    finally:
        b.close()
    b = Broker(path)
    try:
        for peer in (OWNER, BUILDER, REVIEWER):
            b.register(peer, peer + "-token")
        restored = await b.workflows.resume(OWNER, run["id"], pending["version"])
        assert len(restored["stages"]) == 2 and restored["stages"][1]["id"] == successor_id
        assert restored["stages"][1]["channel_observation"]["approved_at"] is None
        assert b.db.execute("SELECT count(*) FROM channels").fetchone()[0] == 2
        assert b.db.execute("SELECT count(*) FROM approval_decisions").fetchone()[0] == 1
    finally:
        b.close()


async def test_clarification_must_resolve_before_projection_and_accepted_artifact_survives_expiry(broker):
    run = await start(broker)
    build = run["stages"][0]
    await broker.decide(build["channel"], True, host_approval=(OWNER, "accept"))
    question = (await broker.receive(BUILDER, build["channel"], timeout=0))["messages"][0]
    clarification = await broker.send(BUILDER, build["channel"], "question", "Confirm format", "clarify", question["id"])
    await broker.send(BUILDER, build["channel"], "answer", build_result(build), "candidate-with-clarification", question["id"])
    waiting = await broker.workflows.resume(OWNER, run["id"], run["version"])
    assert waiting["version"] == run["version"] and len(waiting["stages"]) == 1
    assert waiting["stages"][0]["questions"][0]["id"] == clarification["id"]
    await broker.send(OWNER, build["channel"], "answer", "Format confirmed", "clarify-answer", clarification["id"])
    reviewed = await broker.workflows.resume(OWNER, run["id"], run["version"])
    review = reviewed["stages"][1]
    await answer_stage(broker, review, review_result(review, reviewed["stages"][0]))
    accepted = await broker.workflows.resume(OWNER, run["id"], reviewed["version"])
    broker.test_clock[0] += 86400
    durable = broker.workflows.status(OWNER, run["id"])
    assert durable["state"] == "accepted" and durable["version"] == accepted["version"]
    assert durable["stages"][0]["output"]["candidate_content"] == "candidate"
    assert durable["stages"][1]["channel_observation"]["effective_state"] == "expired"


@pytest.mark.asyncio
async def test_expired_pending_stage_never_executes_or_advances(broker):
    run = await start(broker)
    stage = run["stages"][0]
    broker.test_clock[0] += 24 * 60 * 60
    result = await broker.workflows.resume(OWNER, run["id"], run["version"])
    assert result["state"] == "expired"
    assert result["stages"][0]["channel_observation"]["status"] == "expired"
    assert (await broker.receive(BUILDER, stage["channel"], timeout=0))["messages"] == []


@pytest.mark.asyncio
async def test_owner_acl_creation_idempotency_and_version_conflicts(broker):
    run = await start(broker)
    same = await start(broker)
    assert same["id"] == run["id"]
    assert broker.db.execute("SELECT count(*) FROM workflow_runs").fetchone()[0] == 1
    with pytest.raises(ConnectorError) as forbidden:
        broker.workflows.status(BUILDER, run["id"])
    assert forbidden.value.code == "forbidden"
    with pytest.raises(ConnectorError) as mismatch:
        await broker.workflows.start(OWNER, "different", BUILDER, REVIEWER, "creation-key")
    assert mismatch.value.code == "idempotency_conflict"
    with pytest.raises(ConnectorError) as stale:
        await broker.workflows.resume(OWNER, run["id"], run["version"] - 1)
    assert stale.value.code == "workflow_conflict"


@pytest.mark.asyncio
async def test_open_before_link_crash_reuses_the_single_deterministic_channel(broker, monkeypatch):
    original_open = broker.open
    opened_then_failed = False

    async def fail_after_open(*args, **kwargs):
        nonlocal opened_then_failed
        channel = await original_open(*args, **kwargs)
        if not opened_then_failed:
            opened_then_failed = True
            raise RuntimeError("simulated crash after broker open")
        return channel

    monkeypatch.setattr(broker, "open", fail_after_open)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await start(broker)
    run_id = broker.db.execute("SELECT id FROM workflow_runs").fetchone()[0]
    stage_key = broker.db.execute("SELECT request_key FROM workflow_stages").fetchone()[0]
    first_channel = broker.db.execute("SELECT id FROM channels WHERE open_key=?", (stage_key,)).fetchone()[0]
    run = await broker.workflows.start(OWNER, "Implement the requested change", BUILDER, REVIEWER, "creation-key")
    assert run["stages"][0]["channel"] == first_channel
    assert broker.db.execute("SELECT count(*) FROM channels WHERE open_key=?", (stage_key,)).fetchone()[0] == 1
    assert run["id"] == run_id


@pytest.mark.asyncio
async def test_successor_insert_failure_rolls_back_prior_stage_and_run(broker):
    run = await start(broker)
    build = run["stages"][0]
    await answer_stage(broker, build, build_result(build))
    broker.db.execute("""CREATE TRIGGER fail_review BEFORE INSERT ON workflow_stages
        WHEN NEW.role='review' BEGIN SELECT RAISE(ABORT, 'injected successor failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected successor failure"):
        await broker.workflows.resume(OWNER, run["id"], run["version"])
    unchanged = broker.workflows.status(OWNER, run["id"])
    assert unchanged["version"] == run["version"]
    assert unchanged["state"] == "waiting" and len(unchanged["stages"]) == 1
    assert unchanged["stages"][0]["state"] == "prepared"
    broker.db.execute("DROP TRIGGER fail_review")
    advanced = await broker.workflows.resume(OWNER, run["id"], run["version"])
    assert len(advanced["stages"]) == 2 and advanced["stages"][0]["state"] == "completed"


@pytest.mark.asyncio
async def test_oversized_generated_stage_fails_without_partial_successor(broker):
    run = await start(broker, plan="p" * 4000)
    build = run["stages"][0]
    await answer_stage(broker, build, build_result(build, "x" * 6000, "y" * 6000))
    result = await broker.workflows.resume(OWNER, run["id"], run["version"])
    assert result["state"] == "invalid_result"
    assert result["stages"][0]["error"] == "workflow_payload_too_large"
    assert len(result["stages"]) == 1


@pytest.mark.asyncio
async def test_cancel_commit_recovers_pending_channel_after_revoke_crash(broker, monkeypatch):
    run = await start(broker)
    channel = run["stages"][0]["channel"]
    original_revoke = broker.revoke

    async def fail_once(cid):
        if not getattr(fail_once, "failed", False):
            fail_once.failed = True
            raise RuntimeError("simulated crash before revoke")
        return await original_revoke(cid)

    monkeypatch.setattr(broker, "revoke", fail_once)
    with pytest.raises(RuntimeError, match="before revoke"):
        await broker.workflows.cancel(OWNER, run["id"], run["version"])
    assert broker.workflows.status(OWNER, run["id"])["state"] == "cancelled"
    await broker.workflows.recover_cancelled()
    assert broker.channel(channel)["status"] == "revoked"
    assert broker.db.execute("SELECT channel FROM workflow_stages WHERE run=?", (run["id"],)).fetchone()[0] == channel


@pytest.mark.asyncio
async def test_cancel_recovers_channel_opened_before_stage_link(broker, monkeypatch):
    original_open = broker.open
    failed = False

    async def open_then_crash(*args, **kwargs):
        nonlocal failed
        channel = await original_open(*args, **kwargs)
        if not failed:
            failed = True
            raise RuntimeError("open committed before crash")
        return channel

    monkeypatch.setattr(broker, "open", open_then_crash)
    with pytest.raises(RuntimeError, match="open committed"):
        await start(broker)
    run_id = broker.db.execute("SELECT id FROM workflow_runs").fetchone()[0]
    run = broker.workflows.status(OWNER, run_id)
    await broker.workflows.cancel(OWNER, run_id, run["version"])
    channel = broker.db.execute("SELECT id FROM channels").fetchone()[0]
    assert broker.channel(channel)["status"] == "revoked"
    assert broker.db.execute("SELECT channel FROM workflow_stages WHERE run=?", (run_id,)).fetchone()[0] == channel


@pytest.mark.asyncio
async def test_binding_change_blocks_approval_and_reception(broker):
    run = await start(broker)
    stage = run["stages"][0]
    broker.conversations.registrations[BUILDER] = {**REGISTRATIONS[BUILDER], "session_id": "33333333-3333-4333-8333-333333333333"}
    with pytest.raises(ConnectorError) as changed:
        await broker.decide(stage["channel"], True, host_approval=(OWNER, "accept"))
    assert changed.value.code == "workflow_binding_changed"
    assert broker.db.execute("SELECT count(*) FROM messages WHERE channel=?", (stage["channel"],)).fetchone()[0] == 0

    # Restore, approve, then change again before the responder can receive the initial payload.
    broker.conversations.registrations.pop(BUILDER, None)
    await broker.decide(stage["channel"], True, host_approval=(OWNER, "accept"))
    broker.conversations.registrations[BUILDER] = {"provider": "codex_ingress", "session_id": "33333333-3333-4333-8333-333333333333", "cwd": "/replacement"}
    with pytest.raises(ConnectorError) as changed_on_delivery:
        await broker.receive(BUILDER, stage["channel"], timeout=0)
    assert changed_on_delivery.value.code == "workflow_binding_changed"
    assert broker.db.execute("SELECT count(*) FROM messages WHERE channel=?", (stage["channel"],)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_reviewer_binding_change_does_not_starve_builder_inbox_or_wakeup(broker, tmp_path):
    broker.conversations.registrations[REVIEWER] = REGISTRATIONS[REVIEWER]
    run = await start(broker)
    stale = run["stages"][0]
    await broker.decide(stale["channel"], True, host_approval=(OWNER, "accept"))
    broker.conversations.registrations[REVIEWER] = {
        **REGISTRATIONS[REVIEWER], "session_id": "33333333-3333-4333-8333-333333333333"}

    ordinary = await broker.open(OWNER, BUILDER, "ordinary task", "ordinary-task")
    await broker.decide(ordinary["id"], True, host_approval=(OWNER, "accept"))
    received = await broker.receive(BUILDER, timeout=0)
    assert [message["channel"] for message in received["messages"]] == [ordinary["id"]]
    assert received["blocked_channels"] == [{"channel": stale["channel"], "error": "workflow_binding_changed"}]

    store = WakeStore(tmp_path / "wakeup.sqlite3")
    try:
        dispatcher = Dispatcher(broker, store, {BUILDER: Binding(BUILDER, {"workspace_key": "test"}, object())},
                                clock=lambda: broker.clock(), settle_seconds=0)
        candidates, pending = dispatcher._candidates(BUILDER)
        assert pending is True
        assert [candidate["channel"] for candidate in candidates] == [ordinary["id"]]
        assert any(item["code"] == "workflow_binding_changed" for item in broker.snapshot()["incidents"])
    finally:
        store.close()
