import pytest

from local_ai_connector.core import Broker, ConnectorError


ROUTES = [
    ("codex", "claude"), ("claude", "codex"),
    ("codex", "antigravity"), ("antigravity", "codex"),
    ("claude", "antigravity"), ("antigravity", "claude"),
    ("generic-a", "generic-b"), ("generic-b", "generic-a"),
]


@pytest.mark.parametrize("requester,responder", ROUTES)
async def test_rounds_reuse_approved_channel_across_host_direction_contract(tmp_path, requester, responder):
    now = [1000.0]
    path = tmp_path / "state.sqlite3"
    broker = Broker(path, clock=lambda: now[0])
    broker.register(requester, "requester-token")
    broker.register(responder, "responder-token")
    channel = await broker.open(requester, responder, "original task", "original-key", idle_seconds=10)
    await broker.decide(channel["id"], True)
    first_question = broker.db.execute("SELECT * FROM messages WHERE channel=? AND kind='question'", (channel["id"],)).fetchone()
    first_answer = await broker.send(responder, channel["id"], "answer", "first answer", "first-answer",
                                     reply_to=first_question["id"])
    await broker.finish(responder, channel["id"])
    now[0] += 11
    broker.expire()
    assert broker.channel(channel["id"])["status"] == "closed"

    second_question = await broker.continue_channel(requester, channel["id"], "continue original task", "round-2")
    assert broker.channel(channel["id"])["status"] == "active"
    assert broker.channel(channel["id"])["expires"] == channel["expires"]
    clarification = await broker.send(responder, channel["id"], "question", "Need one detail", "round-2-clarify",
                                      reply_to=second_question["id"])
    clarification_reply = await broker.send(requester, channel["id"], "answer", "The detail", "round-2-detail",
                                            reply_to=clarification["id"])
    assert clarification_reply["reply_to"] == clarification["id"]
    await broker.send(responder, channel["id"], "answer", "second answer", "second-answer",
                      reply_to=second_question["id"])
    result = broker.round_result(requester, channel["id"], second_question["id"])
    assert result["answer"]["body"] == "second answer"
    assert broker.round_result(requester, channel["id"], first_question["id"])["answer"]["id"] == first_answer["id"]
    assert second_question["id"] != first_question["id"]

    await broker.finish(responder, channel["id"])
    now[0] += 11
    broker.expire()
    idle_since = broker.channel(channel["id"])["idle_since"]
    replay = await broker.continue_channel(requester, channel["id"], "continue original task", "round-2")
    assert replay["id"] == second_question["id"]
    assert broker.channel(channel["id"])["status"] == "closed"
    assert broker.channel(channel["id"])["idle_since"] == idle_since
    with pytest.raises(ConnectorError) as conflict:
        await broker.continue_channel(requester, channel["id"], "changed body", "round-2")
    assert conflict.value.code == "idempotency_conflict"
    broker.close()


@pytest.mark.parametrize("terminal", ["expired", "revoked"])
async def test_terminal_channel_never_reopens_or_returns_round_answer(tmp_path, terminal):
    now = [1000.0]
    broker = Broker(tmp_path / "state.sqlite3", clock=lambda: now[0])
    broker.register("requester", "r")
    broker.register("responder", "w")
    channel = await broker.open("requester", "responder", "task", "open", ttl_seconds=10)
    await broker.decide(channel["id"], True)
    question = await broker.continue_channel("requester", channel["id"], "round", "round")
    answer = await broker.send("responder", channel["id"], "answer", "secret result", "answer",
                               reply_to=question["id"])
    if terminal == "expired":
        now[0] += 11
        broker.expire()
    else:
        await broker.revoke(channel["id"])
    with pytest.raises(ConnectorError) as unavailable:
        await broker.continue_channel("requester", channel["id"], "round", "round")
    assert unavailable.value.code == "channel_unavailable"
    with pytest.raises(ConnectorError) as terminal_send:
        await broker.send("responder", channel["id"], "answer", "secret result", "answer",
                          reply_to=question["id"])
    assert terminal_send.value.code == "channel_unavailable"
    result = broker.round_result("requester", channel["id"], question["id"])
    assert result["state"] == terminal and result["answer"] is None and result["questions"] == []
    assert answer["body"] == "secret result"
    broker.close()


async def test_only_requester_can_continue_and_restart_replays_same_round(tmp_path):
    path = tmp_path / "state.sqlite3"
    broker = Broker(path)
    broker.register("a", "a-token")
    broker.register("b", "b-token")
    channel = await broker.open("a", "b", "original", "open")
    await broker.decide(channel["id"], True)
    with pytest.raises(ConnectorError) as forbidden:
        await broker.continue_channel("b", channel["id"], "wrong direction", "round")
    assert forbidden.value.code == "forbidden"
    first = await broker.continue_channel("a", channel["id"], "round", "round")
    broker.close()

    restarted = Broker(path)
    restarted.register("a", "a-token")
    restarted.register("b", "b-token")
    replay = await restarted.continue_channel("a", channel["id"], "round", "round")
    assert replay["id"] == first["id"]
    with pytest.raises(ConnectorError) as conflict:
        await restarted.continue_channel("a", channel["id"], "different", "round")
    assert conflict.value.code == "idempotency_conflict"
    restarted.close()
