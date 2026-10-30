"""The chat that started a task is woken for its result, and only that chat can see the result.

Synthetic: FakeHost stands in for the host adapters; no real host is contacted.
"""
import httpx
import pytest

from local_ai_connector.core import ConnectorError
from local_ai_connector.wakeup import Binding
from test_http import app  # noqa: F401
from test_wakeup import CHAT_A, CredentialHost, FakeHost, bind_chat, env, pinned  # noqa: F401

ORIGIN = "claude:12345678-1234-4123-8123-123456789abc"
OTHER = "claude:87654321-4321-4321-8321-cba987654321"


def asker_wakes(e, host):
    """A dispatcher that wakes zcode (the worker) and asker's own chats (the requester)."""
    d = e.dispatcher()
    d.bindings["asker"] = Binding("asker", {}, host, pinned=True)
    return d


async def answered(e, d, key, body="done"):
    """Open a task from ORIGIN, approve it, wake and answer it on the worker side."""
    channel = (await e.broker.open("asker", "zcode", "what is 17+25?", key, session=ORIGIN))["id"]
    await e.broker.decide(channel, True)
    await d.step()
    question = (await e.broker.receive("zcode", channel, timeout=0))["messages"][0]
    answer = await e.broker.send("zcode", channel, "answer", body, key + "-answer", question["id"])
    return channel, answer


async def test_the_result_wakes_the_chat_that_started_the_task_once(env):
    host = FakeHost()
    host.identity = {"session": ORIGIN}
    d = asker_wakes(env, host)
    channel, answer = await answered(env, d, "result-wake")
    await d.step()
    assert len(host.sent) == 1

    received = await env.broker.receive("asker", channel, timeout=0, session=ORIGIN)
    assert [m["id"] for m in received["messages"]] == [answer["id"]]
    await d.step()
    assert len(host.sent) == 1


async def test_a_workers_question_wakes_the_chat_that_started_the_task(env):
    host = FakeHost()
    host.identity = {"session": ORIGIN}
    d = asker_wakes(env, host)
    channel = (await env.broker.open("asker", "zcode", "what is 17+25?", "ask-back", session=ORIGIN))["id"]
    await env.broker.decide(channel, True)
    await d.step()
    await env.broker.receive("zcode", channel, timeout=0)
    await env.broker.send("zcode", channel, "question", "In which base?", "clarify")
    await d.step()
    assert len(host.sent) == 1


async def test_only_the_chat_that_started_a_task_sees_its_result(env):
    d = env.dispatcher()
    channel, _ = await answered(env, d, "scoped")
    for call in (lambda: env.broker.receive("asker", channel, timeout=0, session=OTHER),
                 lambda: env.broker.continue_channel("asker", channel, "again", "next", session=OTHER),
                 lambda: env.broker.finish("asker", channel, session=OTHER),
                 lambda: env.broker.open("asker", "zcode", "what is 17+25?", "scoped", session=OTHER)):
        with pytest.raises(ConnectorError, match="another chat"):
            await call()
    with pytest.raises(ConnectorError, match="another chat"):
        env.broker.delegation_result("asker", channel, session=OTHER)
    assert (await env.broker.receive("asker", timeout=0, session=OTHER))["messages"] == []
    assert env.broker.delegation_result("asker", channel, session=ORIGIN)["answer"]["body"] == "done"


async def test_a_result_already_read_or_read_during_the_host_check_is_not_woken(env):
    host = FakeHost()
    host.identity = {"session": ORIGIN}
    d = asker_wakes(env, host)
    channel, _ = await answered(env, d, "read-first")
    env.broker.delegation_result("asker", channel, session=ORIGIN)  # connector_delegate's own wait read it
    await d.step()
    assert host.sent == []

    channel, _ = await answered(env, d, "read-during-check")
    confirm = host.confirm

    async def read_then_confirm(target):
        env.broker.delegation_result("asker", channel, session=ORIGIN)
        return await confirm(target)

    host.confirm = read_then_confirm
    await d.step()
    assert host.sent == []


async def test_a_fixed_chat_binding_is_not_woken_for_a_result_it_cannot_read(env):
    host = FakeHost()
    host.identity = {"workspace_key": "relay"}
    d = env.dispatcher()
    d.bindings["asker"] = Binding("asker", dict(host.identity), host)
    await answered(env, d, "fixed")
    await d.step()
    assert host.sent == []


async def test_a_revoked_task_never_wakes_for_its_result(env):
    host = FakeHost()
    host.identity = {"session": ORIGIN}
    d = asker_wakes(env, host)
    channel, _ = await answered(env, d, "revoked")
    await env.broker.revoke(channel)
    await d.step()
    assert host.sent == []


async def test_a_follow_up_result_wakes_the_same_chat_until_it_is_read(env):
    host = FakeHost()
    host.identity = {"session": ORIGIN}
    d = asker_wakes(env, host)
    channel, _ = await answered(env, d, "follow-up", body="first")
    env.broker.delegation_result("asker", channel, session=ORIGIN)
    await env.broker.finish("asker", channel, session=ORIGIN)
    question = await env.broker.continue_channel("asker", channel, "again", "next", session=ORIGIN)
    await env.broker.send("zcode", channel, "answer", "second", "second-answer", question["id"])
    await d.step()
    assert len(host.sent) == 1
    assert env.broker.round_result("asker", channel, question["id"], session=ORIGIN)["answer"]["body"] == "second"
    await d.step()
    assert len(host.sent) == 1


async def test_on_windows_claude_only_the_bound_chat_is_woken_for_a_result(pinned):
    """Claude's Windows inbox needs the chat's own token, which only the bound chat hands over. A result
    for another chat waits to be collected and does not hold up the bound chat's own wake-ups."""
    e = pinned
    e.broker.bindings.bindable.add("asker")
    host = CredentialHost()
    d = e.dispatcher()
    d.bindings["asker"] = Binding("asker", {}, host, pinned=True, credential_required=True)
    request = await e.broker.request_binding("asker", ORIGIN, "Origin", "bind-origin")
    await e.broker.decide_binding("asker", ORIGIN, request["request"], "accept", "host_elicitation")
    e.broker.enroll("asker", ORIGIN, "origin-token")
    await bind_chat(e.broker, CHAT_A, "Worker")

    other = (await e.broker.open("asker", "zcode", "first", "from-other", session=OTHER))["id"]
    mine = (await e.broker.open("asker", "zcode", "second", "from-origin", session=ORIGIN))["id"]
    for channel in (other, mine):
        await e.broker.decide(channel, True)
        question = e.broker.db.execute("SELECT id FROM messages WHERE channel=?", (channel,)).fetchone()[0]
        await e.broker.send("zcode", channel, "answer", "done", "answer-" + channel, question, session=CHAT_A)
    await d.step()

    assert [target for kind, target in host.targets if kind == "send"] == [{"session": ORIGIN}]
    assert ("credential", {"token": "origin-token", "inbox": None}) in host.targets
    assert "wake_target_not_bound" in {i["code"] for i in e.broker.snapshot()["incidents"]}
    assert (await e.broker.receive("asker", other, timeout=0, session=OTHER))["messages"]


async def test_http_result_needs_the_chat_that_started_the_task_and_counts_as_received(app):  # noqa: F811
    observed = []
    app.state.broker.observers.append(lambda peer, rows: observed.extend((peer, r["id"]) for r in rows))
    a, b = {"Authorization": "Bearer a-secret"}, {"Authorization": "Bearer b-secret"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
        opened = await http.post("/call", headers=a, json={"action": "open", "target": "b", "body": "test",
                                                           "key": "origin-http", "session": ORIGIN})
        channel = opened.json()["id"]
        assert (await http.post("/admin", headers={"Authorization": "Bearer admin-secret"},
                                json={"action": "approve", "channel": channel})).status_code == 200
        question = (await http.post("/call", headers=b, json={"action": "receive", "channel": channel,
                                                              "timeout": 0})).json()["messages"][0]
        answer = (await http.post("/call", headers=b, json={"action": "send", "channel": channel, "kind": "answer",
                                                            "body": "done", "key": "result",
                                                            "reply_to": question["id"]})).json()
        wrong = await http.post("/call", headers=a, json={"action": "result", "channel": channel, "session": OTHER})
        assert wrong.status_code == 409 and wrong.json()["error"] == "wrong_session"
        result = await http.post("/call", headers=a, json={"action": "result", "channel": channel, "session": ORIGIN})
        assert result.json()["answer"]["id"] == answer["id"]
    assert ("a", answer["id"]) in observed
