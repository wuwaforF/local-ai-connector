"""Exact-chat bindings: approval pins the target, re-binding never redirects, only the pinned chat receives.

Sessions here stand for host-supplied chat identities (Codex thread metadata, Claude session
environment). Tool arguments never reach these parameters.
"""
import asyncio
import json

import pytest

from local_ai_connector.bindings import CREDENTIAL_TTL
from local_ai_connector.core import Broker, ConnectorError

CHAT_A, CHAT_B, CHAT_C = "codex:thread-a", "codex:thread-b", "codex:thread-c"
CLAUDE_CHAT = "claude:session-1"
TOKEN = "inbox-token-7b1f-secret"


@pytest.fixture
def env(tmp_path):
    now = [1000.0]
    broker = Broker(tmp_path / "state.sqlite3", clock=lambda: now[0])
    for peer in ("codex", "claude", "antigravity"):
        broker.register(peer, peer + "-token")
    broker.bindings.bindable.update({"codex", "claude"})
    yield type("Env", (), {"broker": broker, "now": now, "path": tmp_path})()
    broker.close()


async def bind(broker, endpoint, session, label, key=None, decision="accept"):
    request = await broker.request_binding(endpoint, session, label, key or f"bind-{session}-{label}")
    return await broker.decide_binding(endpoint, session, request["request"], decision, "host_elicitation")


async def task(broker, key="t1", **kwargs):
    return await broker.open("claude", "codex", "What is 17+25?", key, **kwargs)


async def approve(broker, channel):
    return await broker.decide(channel, True, host_approval=("claude", "accept"), host_approval_source="host_tool_permission")


def ids(result):
    return [m["channel"] for m in result["messages"]]


# --- binding requests --------------------------------------------------------------------

async def test_bind_needs_trusted_identity_and_approval_in_the_requesting_chat(env):
    b = env.broker
    with pytest.raises(ConnectorError) as error:
        await b.request_binding("codex", None, "Reviewer", "k")
    assert error.value.code == "missing_session_identity"
    with pytest.raises(ConnectorError) as error:
        await b.request_binding("antigravity", "x:y", "Worker", "k")
    assert error.value.code == "binding_unsupported"
    request = await b.request_binding("codex", CHAT_A, "Reviewer", "k")
    assert request["state"] == "pending" and request["task_authorized"] is False
    # Another chat cannot approve a binding of chat A.
    with pytest.raises(ConnectorError) as error:
        await b.decide_binding("codex", CHAT_B, request["request"], "accept", "host_elicitation")
    assert error.value.code == "wrong_session"
    assert b.bindings.current("codex") is None
    # A retry with the same key returns the same request instead of a second one.
    assert (await b.request_binding("codex", CHAT_A, "Reviewer", "k"))["request"] == request["request"]
    approved = await b.decide_binding("codex", CHAT_A, request["request"], "accept", "host_elicitation")
    assert approved["state"] == "approved" and approved["revision"] == 1
    assert b.bindings.current("codex")["session"] == CHAT_A


async def test_declined_or_expired_bind_keeps_the_current_binding(env):
    b = env.broker
    await bind(b, "codex", CHAT_A, "Reviewer")
    declined = await bind(b, "codex", CHAT_B, "Other", decision="decline")
    assert declined["state"] == "declined" and b.bindings.current("codex")["session"] == CHAT_A
    late = await b.request_binding("codex", CHAT_B, "Late", "late")
    env.now[0] += 301
    with pytest.raises(ConnectorError) as error:
        await b.decide_binding("codex", CHAT_B, late["request"], "accept", "host_elicitation")
    assert error.value.code == "binding_request_expired"
    assert b.bindings.current("codex")["session"] == CHAT_A and b.bindings.revision("codex") == 1


async def test_concurrent_bind_requests_cannot_both_win(env):
    b = env.broker
    first = await b.request_binding("codex", CHAT_A, "A", "a")
    second = await b.request_binding("codex", CHAT_B, "B", "b")
    await b.decide_binding("codex", CHAT_A, first["request"], "accept", "host_elicitation")
    # B was requested against revision 0; approving it would silently replace A.
    with pytest.raises(ConnectorError) as error:
        await b.decide_binding("codex", CHAT_B, second["request"], "accept", "host_elicitation")
    assert error.value.code == "binding_changed"
    assert b.bindings.current("codex")["session"] == CHAT_A


# --- approval pins the target ------------------------------------------------------------

async def test_task_to_an_unbound_worker_is_refused_before_approval(env):
    with pytest.raises(ConnectorError) as error:
        await task(env.broker)
    assert error.value.code == "target_not_bound"
    assert env.broker.db.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0


async def test_rebinding_between_request_and_approval_refuses_and_delivers_nothing(env):
    b = env.broker
    await bind(b, "codex", CHAT_A, "Reviewer")
    opened = await task(b)
    assert opened["target_chat"] == {"label": "Reviewer", "chat": "codex:thread-a", "revision": 1, "pinned": False}
    await bind(b, "codex", CHAT_B, "New reviewer")
    with pytest.raises(ConnectorError) as error:
        await approve(b, opened["id"])
    assert error.value.code == "target_binding_changed"
    assert b.channel(opened["id"])["status"] == "revoked"
    assert b.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    for chat in (CHAT_A, CHAT_B):
        assert (await b.receive("codex", timeout=0, session=chat))["messages"] == []


async def test_rebinding_after_approval_never_moves_the_approved_task(env):
    b = env.broker
    await bind(b, "codex", CHAT_A, "Reviewer")
    first = await task(b, "t1")
    await approve(b, first["id"])
    assert b.bindings.target_view(first["id"])["pinned"] is True
    await bind(b, "codex", CHAT_B, "New reviewer")
    second = await task(b, "t2")
    await approve(b, second["id"])
    assert ids(await b.receive("codex", timeout=0, session=CHAT_A)) == [first["id"]]
    assert ids(await b.receive("codex", timeout=0, session=CHAT_B)) == [second["id"]]
    # Follow-up rounds of the first task still go to chat A.
    question = (await b.receive("codex", first["id"], timeout=0, session=CHAT_A))["messages"][0]
    await b.send("codex", first["id"], "answer", "42", "a1", question["id"], session=CHAT_A)
    await b.continue_channel("claude", first["id"], "And 18+24?", "round-2")
    assert ids(await b.receive("codex", first["id"], after=question["seq"], timeout=0, session=CHAT_A)) == [first["id"]]
    with pytest.raises(ConnectorError) as error:
        await b.receive("codex", first["id"], timeout=0, session=CHAT_B)
    assert error.value.code == "wrong_session"


@pytest.mark.parametrize("approval_first", [True, False])
async def test_concurrent_approval_and_rebinding_resolve_consistently(env, approval_first):
    b = env.broker
    await bind(b, "codex", CHAT_A, "Reviewer")
    opened = await task(b)
    request = await b.request_binding("codex", CHAT_B, "New reviewer", "race")
    rebind = b.decide_binding("codex", CHAT_B, request["request"], "accept", "host_elicitation")
    approval = approve(b, opened["id"])
    results = await asyncio.gather(*((approval, rebind) if approval_first else (rebind, approval)),
                                   return_exceptions=True)
    channel = b.channel(opened["id"])
    target = b.bindings.target(opened["id"])
    assert b.bindings.current("codex")["session"] == CHAT_B
    if approval_first:
        # Approved while A was bound: pinned to A and delivered only there.
        assert channel["status"] == "active" and target["session"] == CHAT_A and target["pinned_at"] is not None
        assert ids(await b.receive("codex", timeout=0, session=CHAT_A)) == [opened["id"]]
    else:
        # The binding changed first: the approval is refused, never redirected to B.
        assert isinstance(results[1], ConnectorError) and results[1].code == "target_binding_changed"
        assert channel["status"] == "revoked"
    assert (await b.receive("codex", timeout=0, session=CHAT_B))["messages"] == []


async def test_tasks_must_name_the_current_binding_where_the_prompt_shows_only_arguments(env):
    b = env.broker
    await bind(b, "codex", CHAT_A, "Reviewer")
    with pytest.raises(ConnectorError) as error:
        await task(b, require_target=True)
    assert error.value.code == "target_chat_required"
    with pytest.raises(ConnectorError) as error:
        await task(b, require_target=True, expected_revision=1, expected_label="Someone else")
    assert error.value.code == "target_binding_changed"
    with pytest.raises(ConnectorError) as error:
        await task(b, expected_revision=2)
    assert error.value.code == "target_binding_changed"
    opened = await task(b, require_target=True, expected_revision=1, expected_label="Reviewer")
    # A retry with the same key cannot claim a different revision.
    with pytest.raises(ConnectorError) as error:
        await task(b, expected_revision=5)
    assert error.value.code == "idempotency_conflict"
    assert (await task(b, expected_revision=1))["id"] == opened["id"]


# --- only the pinned chat receives and answers -------------------------------------------

async def test_other_chats_of_the_same_endpoint_cannot_receive_or_answer(env):
    b = env.broker
    await bind(b, "codex", CHAT_A, "Reviewer")
    opened = await task(b)
    await approve(b, opened["id"])
    # Same endpoint credential, different chat: nothing visible, explicit access refused.
    assert (await b.receive("codex", timeout=0, session=CHAT_C))["messages"] == []
    assert (await b.receive("codex", timeout=0, session=None))["messages"] == []
    with pytest.raises(ConnectorError) as error:
        await b.receive("codex", opened["id"], timeout=0, session=None)
    assert error.value.code == "wrong_session"
    question = (await b.receive("codex", timeout=0, session=CHAT_A))["messages"][0]
    for chat in (CHAT_C, None):
        with pytest.raises(ConnectorError) as error:
            await b.send("codex", opened["id"], "answer", "fake", f"x-{chat}", question["id"], session=chat)
        assert error.value.code == "wrong_session"
    await b.send("codex", opened["id"], "answer", "42", "a1", question["id"], session=CHAT_A)
    # The initiating side is not pinned: the requester reads the real answer.
    assert b.delegation_result("claude", opened["id"])["answer"]["body"] == "42"


async def test_status_reports_the_binding_and_whether_this_chat_is_bound(env):
    b = env.broker
    await bind(b, "codex", CHAT_A, "Reviewer")
    workers = {w["id"]: w for w in b.snapshot("codex", CHAT_A)["workers"]}
    assert workers["codex"]["binding"] == {"bound": True, "revision": 1, "label": "Reviewer",
                                           "chat": "codex:thread-a", "this_chat_bound": True}
    assert {w["id"]: w for w in b.snapshot("codex", CHAT_B)["workers"]}["codex"]["binding"]["this_chat_bound"] is False
    assert "binding" not in workers["antigravity"]
    with pytest.raises(ConnectorError) as error:
        await b.unbind("codex", CHAT_B)
    assert error.value.code == "wrong_session"
    assert (await b.unbind("codex", CHAT_A))["revision"] == 2
    assert b.bindings.current("codex") is None


# --- session-scoped delivery credentials -------------------------------------------------

async def test_delivery_credentials_are_session_scoped_and_go_stale(env, tmp_path):
    b = env.broker
    creds = b.bindings.credentials
    await bind(b, "claude", CLAUDE_CHAT, "Claude worker")
    with pytest.raises(ConnectorError) as error:
        b.enroll("claude", "claude:other-session", TOKEN)
    assert error.value.code == "not_enrolled_target"
    b.enroll("claude", CLAUDE_CHAT, TOKEN)
    assert creds.get("claude", CLAUDE_CHAT, 1) == TOKEN
    assert creds.get("claude", CLAUDE_CHAT, 2) is None  # a different revision never matches
    # A newer enrollment replaces the old token.
    b.enroll("claude", CLAUDE_CHAT, TOKEN + "-new")
    assert creds.get("claude", CLAUDE_CHAT, 1) == TOKEN + "-new"
    # Re-binding, even back to the same chat, makes the token stale until the chat enrolls again.
    await bind(b, "claude", "claude:session-2", "Other", key="x")
    await bind(b, "claude", CLAUDE_CHAT, "Claude worker", key="back")
    assert creds.get("claude", CLAUDE_CHAT, 3) is None
    b.enroll("claude", CLAUDE_CHAT, TOKEN)
    env.now[0] += CREDENTIAL_TTL + 1
    assert creds.get("claude", CLAUDE_CHAT, 3) is None  # expired
    b.enroll("claude", CLAUDE_CHAT, TOKEN)
    await b.unbind("claude", CLAUDE_CHAT)
    assert creds.get("claude", CLAUDE_CHAT, 3) is None


async def test_delivery_credentials_are_never_persisted_or_exposed(env):
    b = env.broker
    await bind(b, "claude", CLAUDE_CHAT, "Claude worker")
    b.enroll("claude", CLAUDE_CHAT, TOKEN)
    with pytest.raises(ConnectorError) as error:
        b.enroll("claude", "claude:intruder", TOKEN)
    exposed = [json.dumps(b.snapshot()), json.dumps(b.snapshot("claude", CLAUDE_CHAT)), repr(b.bindings.credentials),
               str(error.value), json.dumps([dict(r) for r in b.db.execute("SELECT * FROM incidents")])]
    assert not any(TOKEN in text for text in exposed)
    b.db.commit()
    database = (env.path / "state.sqlite3").read_bytes() + (
        (env.path / "state.sqlite3-wal").read_bytes() if (env.path / "state.sqlite3-wal").exists() else b"")
    assert TOKEN.encode() not in database
    # A restarted service has no credentials until the bound chat enrolls again.
    restarted = Broker(env.path / "state.sqlite3", clock=lambda: env.now[0])
    try:
        restarted.bindings.bindable.add("claude")
        assert restarted.bindings.credentials.get("claude", CLAUDE_CHAT, 1) is None
    finally:
        restarted.close()
