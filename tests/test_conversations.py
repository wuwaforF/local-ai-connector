import itertools
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from local_ai_connector.core import Broker, ConnectorError
from local_ai_connector.conversations import CREATE, SEND, handle_hook


SESSION = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
REGISTRATION = {"provider": "codex_ingress", "session_id": SESSION, "cwd": "/worker"}


@pytest.fixture
def broker(tmp_path):
    broker = Broker(tmp_path / "state.sqlite3", clock=lambda: 100,
                    conversations={"codex": REGISTRATION})
    for peer in ("codex", "antigravity", "claude"):
        broker.register(peer, peer + "-token")
    yield broker
    broker.close()


def event(plan, name="PermissionRequest", **changes):
    return {"hook_event_name": name, "session_id": SESSION, "cwd": "/worker", "turn_id": "turn-1",
            "tool_name": plan["tool"], "tool_input": plan["arguments"], **changes}


def hook(broker, value):
    return handle_hook(broker.db, value, "codex", SESSION, "/worker", 100)


def receipt(plan, thread=CHILD):
    result = {"threadId": thread, "hostId": "local"}
    return event(plan, "PostToolUse", tool_response={"content": [{"type": "text", "text": json.dumps(result)}], "isError": False})


async def approved(broker, mode="new", key="one"):
    task = await broker.open("antigravity", "codex", "Open a new chat and answer the task", key,
                             conversation_mode=mode)
    await broker.decide(task["id"], True, host_approval=("antigravity", "accept"))
    q = (await broker.receive("codex", task["id"], timeout=0))["messages"][0]
    return task, q


async def test_create_then_continue_uses_one_authorization_and_same_child(broker):
    task, q = await approved(broker)
    plan = q["native_execution"]
    assert plan["tool"] == CREATE and plan["arguments"]["prompt"].endswith(task["original"])
    assert plan["arguments"]["target"]["type"] == "projectless"
    with pytest.raises(ConnectorError, match="receipt"):
        await broker.send("codex", task["id"], "answer", "ingress substitute", "bad", q["id"])
    hook(broker, event(plan))
    hook(broker, receipt(plan))
    await broker.send("codex", task["id"], "answer", "native answer", "answer", q["id"])
    received = (await broker.receive("antigravity", task["id"], timeout=0))["messages"][0]
    assert received["conversation"]["thread_id"] == CHILD
    await broker.finish("antigravity", task["id"])
    broker.db.execute("UPDATE channels SET status='closed' WHERE id=?", (task["id"],))
    broker.db.commit()
    next_q = await broker.continue_channel("antigravity", task["id"], "Add 15", "round-two")
    delivery = (await broker.receive("codex", task["id"], after=q["seq"], timeout=0))["messages"][0]
    followup = delivery["native_execution"]
    assert followup["tool"] == SEND
    assert followup["arguments"] == {"threadId": CHILD, "prompt": "Add 15"}
    hook(broker, event(followup))
    hook(broker, receipt(followup))
    await broker.send("codex", task["id"], "answer", "native follow-up", "next-answer", next_q["id"])
    result = broker.round_result("antigravity", task["id"], next_q["id"])
    assert result["conversation"] == {"mode": "new", "created": True, "thread_id": CHILD, "host_id": "local"}
    assert broker.db.execute("SELECT count(*) FROM approval_decisions").fetchone()[0] == 1
    assert broker.db.execute("SELECT count(*) FROM native_conversations").fetchone()[0] == 1


async def test_updated_hook_preserves_running_broker_with_previous_schema(broker):
    task, q = await approved(broker)
    broker.db.execute('DROP TABLE native_existing_routes')
    broker.db.commit()
    plan = q['native_execution']
    hook(broker, event(plan))
    hook(broker, receipt(plan))
    assert broker.db.execute('SELECT thread_id FROM native_conversations').fetchone()[0] == CHILD
    assert not broker.db.execute("SELECT 1 FROM sqlite_master WHERE name='native_existing_routes'").fetchone()


@pytest.mark.parametrize("requester,target", list(itertools.permutations(("codex", "antigravity", "claude"), 2)))
async def test_all_directions_share_explicit_mode_and_capability_check(broker, requester, target):
    if target == "codex":
        task = await broker.open(requester, target, "new native task", "new", conversation_mode="new")
        assert task["conversation"]["created"] is False
    else:
        with pytest.raises(ConnectorError) as error:
            await broker.open(requester, target, "new native task", "new", conversation_mode="new")
        assert error.value.code == "new_conversation_unsupported"
        assert broker.db.execute("SELECT count(*) FROM channels").fetchone()[0] == 0
    task = await broker.open(requester, target, "existing task", "existing", conversation_mode="existing")
    assert task["conversation"] == {"mode": "existing", "created": False}


@pytest.mark.parametrize("field,value", [("cwd", "/wrong"), ("session_id", CHILD),
                                       ("tool_name", SEND), ("tool_input", {"prompt": "changed"})])
async def test_hook_cannot_expand_exact_native_authority(broker, field, value):
    _, q = await approved(broker)
    with pytest.raises(ConnectorError):
        hook(broker, event(q["native_execution"], **{field: value}))
    assert broker.db.execute("SELECT state FROM native_operations").fetchone()[0] == "prepared"


@pytest.mark.parametrize("status", ["revoked", "expired", "closed", "denied"])
async def test_ended_authority_cannot_create(broker, status):
    task, q = await approved(broker)
    with broker.db:
        broker.db.execute("UPDATE channels SET status=? WHERE id=?", (status, task["id"]))
    with pytest.raises(ConnectorError):
        hook(broker, event(q["native_execution"]))


async def test_replay_mode_change_and_uncertain_creation_never_duplicate(broker):
    task, q = await approved(broker)
    with pytest.raises(ConnectorError) as error:
        await broker.open("antigravity", "codex", task["original"], "one", conversation_mode="existing")
    assert error.value.code == "idempotency_conflict"
    hook(broker, event(q["native_execution"]))
    with pytest.raises(ConnectorError):
        hook(broker, event(q["native_execution"]))
    repeated = (await broker.receive("codex", task["id"], timeout=0))["messages"][0]
    assert repeated["native_execution"]["state"] == "intent"
    assert "arguments" not in repeated["native_execution"]
    bad = receipt(q["native_execution"])
    bad["tool_response"] = {"isError": True, "content": []}
    hook(broker, bad)
    assert broker.conversations.describe(task["id"])["created"] is False
    with pytest.raises(ConnectorError):
        await broker.send("codex", task["id"], "answer", "unconfirmed", "bad", q["id"])


async def test_receipt_replay_is_idempotent_and_cannot_rebind(broker):
    task, q = await approved(broker)
    plan = q["native_execution"]
    with pytest.raises(ConnectorError):
        hook(broker, receipt(plan))
    hook(broker, event(plan))
    hook(broker, receipt(plan))
    hook(broker, receipt(plan))
    with pytest.raises(ConnectorError):
        hook(broker, receipt(plan, "33333333-3333-4333-8333-333333333333"))
    assert broker.conversations.describe(task["id"])["thread_id"] == CHILD


async def test_pending_or_unregistered_tasks_do_not_mint_native_operations(broker):
    task = await broker.open("antigravity", "codex", "new task", "pending", conversation_mode="new")
    assert (await broker.receive("codex", timeout=0))["messages"] == []
    assert broker.db.execute("SELECT count(*) FROM native_operations").fetchone()[0] == 0
    assert not broker.conversations.describe(task["id"])["created"]


async def test_clarification_answer_is_forwarded_to_the_same_native_child(broker):
    task, q = await approved(broker)
    plan = q["native_execution"]
    hook(broker, event(plan))
    hook(broker, receipt(plan))
    clarification = await broker.send("codex", task["id"], "question", "Which city?", "clarify", q["id"])
    answer = await broker.send("antigravity", task["id"], "answer", "Paris", "clarify-answer", clarification["id"])
    messages = (await broker.receive("codex", task["id"], after=q["seq"], timeout=0))["messages"]
    plan = next(m for m in messages if m["id"] == answer["id"])["native_execution"]
    assert plan["arguments"] == {"threadId": CHILD, "prompt": "Paris"}
    with pytest.raises(ConnectorError):
        await broker.send("codex", task["id"], "answer", "unforwarded", "premature", q["id"])
    hook(broker, event(plan))
    hook(broker, receipt(plan))
    await broker.send("codex", task["id"], "answer", "native Paris answer", "final", q["id"])
    assert broker.delegation_result("antigravity", task["id"])["answer"]["body"] == "native Paris answer"


async def test_identical_followup_text_is_distinct_only_with_new_round_key(broker):
    task, q = await approved(broker)
    hook(broker, event(q["native_execution"]))
    hook(broker, receipt(q["native_execution"]))
    await broker.send("codex", task["id"], "answer", "initial", "first-answer", q["id"])
    for round_number in range(2):
        question = await broker.continue_channel("antigravity", task["id"], "Add 1", str(round_number))
        message = (await broker.receive("codex", task["id"], after=question["seq"] - 1, timeout=0))["messages"][0]
        plan = message["native_execution"]
        hook(broker, event(plan))
        hook(broker, receipt(plan))
        await broker.send("codex", task["id"], "answer", str(round_number), "answer-" + str(round_number), question["id"])
    assert broker.db.execute("SELECT count(*) FROM native_operations WHERE state='acknowledged'").fetchone()[0] == 3


async def test_consumed_intent_survives_broker_restart(broker, tmp_path):
    task, q = await approved(broker)
    hook(broker, event(q["native_execution"]))
    other = Broker(tmp_path / "state.sqlite3", clock=lambda: 100, conversations={"codex": REGISTRATION})
    try:
        replay = (await other.receive("codex", task["id"], timeout=0))["messages"][0]
        assert replay["native_execution"]["state"] == "intent"
        with pytest.raises(ConnectorError):
            hook(other, event(q["native_execution"]))
    finally:
        other.close()


async def test_expiry_is_rechecked_and_malformed_receipt_is_reported(broker):
    task, q = await approved(broker)
    with pytest.raises(ConnectorError):
        handle_hook(broker.db, event(q["native_execution"]), "codex", SESSION, "/worker", task["expires"])
    hook(broker, event(q["native_execution"]))
    hook(broker, event(q["native_execution"], "PostToolUse", tool_response={"content": []}))
    assert broker.delegation_result("antigravity", task["id"])["state"] == "native_execution_unconfirmed"


@pytest.mark.parametrize("case", ["approved", "no_grant", "other_session"])
async def test_installed_hook_entrypoint_scopes_and_consumes_real_store(broker, tmp_path, case):
    broker.clock = time.time
    _, q = await approved(broker)
    payload = event(q["native_execution"])
    if case == "other_session":
        payload["session_id"] = CHILD
    elif case == "no_grant":
        payload["tool_input"]["prompt"] = "Unapproved replacement"
    script = Path(__file__).resolve().parents[1] / "integrations/codex_desktop/conversation_hook.py"
    result = subprocess.run([sys.executable, str(script), "--store", str(tmp_path / "state.sqlite3"),
                             "--peer", "codex", "--session", SESSION, "--cwd", "/worker"],
                            input=json.dumps(payload), text=True, capture_output=True, check=True)
    response = json.loads(result.stdout)
    if case == "other_session":
        assert response == {}
    else:
        assert response["hookSpecificOutput"]["decision"]["behavior"] == ("allow" if case == "approved" else "deny")
    assert broker.db.execute("SELECT state FROM native_operations").fetchone()[0] == ("intent" if case == "approved" else "prepared")
