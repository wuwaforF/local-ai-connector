import json
import sqlite3

import httpx
import pytest

from local_ai_connector.server import create_app


@pytest.fixture
def app(tmp_path):
    config = {
        "admin_token": "admin-secret",
        "peers": {"a": "a-secret", "b": "b-secret", "c": "c-secret"},
        "approval_tokens": {"a": "a-approval", "b": "b-approval"},
    }
    (tmp_path / "server.json").write_text(json.dumps(config))
    for peer in config["peers"]:
        (tmp_path / f"{peer}.json").write_text('{"client":"generic"}')
    app = create_app(tmp_path)
    app.state.broker.clock = lambda: 1000
    yield app
    app.state.broker.close()


@pytest.fixture
async def http(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


async def test_host_accept_is_atomic_audited_and_idempotent(app, http):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Exact original task", "request")
    assert not (await broker.receive("b", timeout=0))["messages"]
    payload = {"channel": channel["id"], "decision": "accept"}
    for _ in range(2):
        response = await http.post("/decision", headers={"Authorization": "Bearer a-approval"}, json=payload)
        assert response.status_code == 200
        assert response.json()["status"] == "active"
    messages = (await broker.receive("b", timeout=0))["messages"]
    assert len(messages) == 1 and messages[0]["body"] == "Exact original task"
    records = [dict(row) for row in broker.db.execute("SELECT * FROM approval_decisions")]
    assert records == [{"channel": channel["id"], "source": "host_elicitation", "peer": "a",
                        "decision": "accept", "created": 1000}]
    assert all(secret not in json.dumps(records) for secret in ("a-approval", "b-approval", "a-secret", "admin-secret"))


@pytest.mark.parametrize("decision", ["decline", "cancel"])
async def test_decline_and_cancel_never_deliver_and_keep_original_decision(app, http, decision):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Never delivered", "request")
    for _ in range(2):
        response = await http.post("/decision", headers={"Authorization": "Bearer a-approval"},
                                   json={"channel": channel["id"], "decision": decision})
        assert response.status_code == 200 and response.json()["status"] == "denied"
    assert not (await broker.receive("b", timeout=0))["messages"]
    row = broker.db.execute("SELECT * FROM approval_decisions").fetchone()
    assert row["decision"] == decision and row["peer"] == "a"
    assert broker.db.execute("SELECT count(*) FROM approval_decisions").fetchone()[0] == 1


@pytest.mark.parametrize("token", [None, "invalid", "a-secret", "b-secret", "admin-secret"])
@pytest.mark.parametrize("source", ["host_elicitation", "host_tool_permission"])
async def test_only_separate_approval_credential_can_submit_decision(app, http, token, source):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Task", "request")
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    response = await http.post("/decision", headers=headers,
                               json={"channel": channel["id"], "decision": "accept", "source": source})
    assert response.status_code == 401
    assert broker.channel(channel["id"])["status"] == "pending"


async def test_approval_token_cannot_use_peer_or_admin_api(app, http):
    headers = {"Authorization": "Bearer a-approval"}
    assert (await http.post("/call", headers=headers, json={"action": "status"})).status_code == 401
    assert (await http.get("/admin", headers=headers)).status_code == 403
    assert (await http.post("/call", headers={"Authorization": "Bearer a-secret"},
                            json={"action": "approve", "channel": "anything"})).status_code == 409


@pytest.mark.parametrize("requester,target", [("b", "a"), ("c", "b")])
@pytest.mark.parametrize("source", ["host_elicitation", "host_tool_permission"])
async def test_approval_credential_is_scoped_to_requester(app, http, requester, target, source):
    broker = app.state.broker
    channel = await broker.open(requester, target, "Other task", "request")
    response = await http.post("/decision", headers={"Authorization": "Bearer a-approval"},
                               json={"channel": channel["id"], "decision": "accept", "source": source})
    assert response.status_code == 403
    assert broker.channel(channel["id"])["status"] == "pending"
    assert not broker.db.execute("SELECT * FROM approval_decisions").fetchall()


@pytest.mark.parametrize("payload", [[], {}, {"channel": 1, "decision": "accept"},
    {"channel": "id", "decision": True}, {"channel": "id", "decision": "approve"},
    {"channel": "id", "decision": "accept", "peer": "a"},
    {"channel": "id", "decision": "accept", "approved": True}])
async def test_decision_payload_boundary(http, payload):
    response = await http.post("/decision", headers={"Authorization": "Bearer a-approval"}, json=payload)
    assert response.status_code == 409 and response.json()["error"] == "invalid_request"


async def test_browser_origin_cannot_submit_decision(app, http):
    channel = await app.state.broker.open("a", "b", "Task", "request")
    response = await http.post("/decision", headers={"Authorization": "Bearer a-approval", "Origin": "https://example.com"},
                               json={"channel": channel["id"], "decision": "accept"})
    assert response.status_code == 403


@pytest.mark.parametrize("ended", ["expired", "revoked"])
@pytest.mark.parametrize("source", ["host_elicitation", "host_tool_permission"])
async def test_authority_ending_during_user_wait_prevents_accept(app, http, ended, source):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Task", "request", ttl_seconds=10)
    if ended == "expired":
        broker.clock = lambda: 1010
    else:
        await broker.revoke(channel["id"])
    response = await http.post("/decision", headers={"Authorization": "Bearer a-approval"},
                               json={"channel": channel["id"], "decision": "accept", "source": source})
    assert response.status_code == 409 and response.json()["error"] == "invalid_state"
    assert broker.channel(channel["id"])["status"] == ended
    assert not broker.db.execute("SELECT * FROM messages").fetchall()
    assert not broker.db.execute("SELECT * FROM approval_decisions").fetchall()


@pytest.mark.parametrize("first,second", [("accept", "decline"), ("cancel", "accept"), ("decline", "cancel")])
async def test_conflicting_repeat_cannot_change_previous_outcome(app, http, first, second):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Task", "request")
    headers = {"Authorization": "Bearer a-approval"}
    assert (await http.post("/decision", headers=headers,
                            json={"channel": channel["id"], "decision": first})).status_code == 200
    response = await http.post("/decision", headers=headers,
                               json={"channel": channel["id"], "decision": second})
    assert response.status_code == 409
    assert broker.db.execute("SELECT decision FROM approval_decisions").fetchone()[0] == first


@pytest.mark.parametrize("source", ["host_elicitation", "host_tool_permission"])
async def test_audit_failure_rolls_back_approval_and_original_message(app, http, source):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Task", "request")
    broker.db.execute("""CREATE TRIGGER reject_audit BEFORE INSERT ON approval_decisions
        BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        await http.post("/decision", headers={"Authorization": "Bearer a-approval"},
                        json={"channel": channel["id"], "decision": "accept", "source": source})
    assert broker.channel(channel["id"])["status"] == "pending"
    assert not broker.db.execute("SELECT * FROM messages").fetchall()


@pytest.mark.parametrize("approval_tokens", [None, [], {"missing": "new-secret"}, {"a": ""},
    {"a": 3}, {"a": "a-secret"}, {"a": "admin-secret"}, {"a": "same", "b": "same"}])
def test_invalid_approval_configuration_fails_before_service_start(tmp_path, approval_tokens):
    config = {"admin_token": "admin-secret", "peers": {"a": "a-secret", "b": "b-secret"},
              "approval_tokens": approval_tokens}
    (tmp_path / "server.json").write_text(json.dumps(config))
    for peer in config["peers"]:
        (tmp_path / f"{peer}.json").write_text('{"client":"generic"}')
    with pytest.raises(ValueError, match="approval_tokens"):
        create_app(tmp_path)


async def test_closed_delegation_returns_original_correlated_answer(app, http):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Task", "request", idle_seconds=1)
    await broker.decide(channel["id"], True)
    question = (await broker.receive("b", timeout=0))["messages"][0]
    answer = await broker.send("b", channel["id"], "answer", "Actual result", "reply", question["id"])
    await broker.finish("a", channel["id"])
    broker.clock = lambda: 1002
    response = await http.post("/call", headers={"Authorization": "Bearer a-secret"},
                               json={"action": "result", "channel": channel["id"]})
    assert response.status_code == 200
    result = response.json()
    assert result["state"] == "closed" and result["channel"] == channel["id"]
    assert result["answer"]["id"] == answer["id"]
    assert result["answer"]["body"] == "Actual result" and result["answer"]["reply_to"] == question["id"]
    assert broker.db.execute("SELECT count(*) FROM channels").fetchone()[0] == 1
    assert broker.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


async def test_closed_result_replay_expires_without_changing_archive(app, http):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Task", "closed-expiry", ttl_seconds=10, idle_seconds=1)
    await broker.decide(channel["id"], True)
    question = (await broker.receive("b", timeout=0))["messages"][0]
    await broker.send("b", channel["id"], "answer", "Private result", "closed-answer", question["id"])
    await broker.finish("a", channel["id"])
    payload = {"action": "result", "channel": channel["id"]}
    headers = {"Authorization": "Bearer a-secret"}
    broker.clock = lambda: 1002
    before = (await http.post("/call", headers=headers, json=payload)).json()
    assert before["state"] == "closed" and before["answer"]["body"] == "Private result"
    broker.clock = lambda: 1010
    after = (await http.post("/call", headers=headers, json=payload)).json()
    assert after == {"state": "expired", "channel": channel["id"]}
    assert broker.channel(channel["id"])["status"] == "closed"
    assert broker.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


@pytest.mark.parametrize("token", ["b-secret", "c-secret"])
async def test_result_is_only_available_to_task_requester(app, http, token):
    channel = await app.state.broker.open("a", "b", "Private task", "request")
    response = await http.post("/call", headers={"Authorization": "Bearer " + token},
                               json={"action": "result", "channel": channel["id"]})
    assert response.status_code == 403


@pytest.mark.parametrize("ended", ["revoked", "expired"])
async def test_result_does_not_replay_content_after_authority_ends(app, http, ended):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Private task", "request", ttl_seconds=10)
    await broker.decide(channel["id"], True)
    question = (await broker.receive("b", timeout=0))["messages"][0]
    await broker.send("b", channel["id"], "answer", "Private result", "reply", question["id"])
    if ended == "revoked":
        await broker.revoke(channel["id"])
    else:
        broker.clock = lambda: 1010
    response = await http.post("/call", headers={"Authorization": "Bearer a-secret"},
                               json={"action": "result", "channel": channel["id"]})
    assert response.status_code == 200
    assert response.json() == {"state": ended, "channel": channel["id"]}


async def test_result_is_empty_while_waiting_for_approval_or_reply(app, http):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Task", "request")
    for state in ("pending", "active"):
        response = await http.post("/call", headers={"Authorization": "Bearer a-secret"},
                                   json={"action": "result", "channel": channel["id"]})
        assert response.json() == {"state": state, "channel": channel["id"]}
        if state == "pending":
            await broker.decide(channel["id"], True)


async def test_result_matches_initial_question_not_another_answer_in_channel(app, http):
    broker = app.state.broker
    channel = await broker.open("a", "b", "Initial task", "request")
    await broker.decide(channel["id"], True)
    initial = (await broker.receive("b", timeout=0))["messages"][0]
    extra = await broker.send("a", channel["id"], "question", "Extra question", "extra")
    await broker.send("b", channel["id"], "answer", "Extra answer", "extra-answer", extra["id"])
    headers = {"Authorization": "Bearer a-secret"}
    payload = {"action": "result", "channel": channel["id"]}
    assert (await http.post("/call", headers=headers, json=payload)).json() == {
        "state": "active", "channel": channel["id"]}
    await broker.send("b", channel["id"], "answer", "Initial result", "initial-answer", initial["id"])
    result = (await http.post("/call", headers=headers, json=payload)).json()
    assert result["answer"]["body"] == "Initial result" and result["answer"]["reply_to"] == initial["id"]


async def test_approval_source_cannot_be_rewritten(app, http):
    c = await app.state.broker.open("a", "b", "Task", "source-test")
    headers = {"Authorization": "Bearer a-approval"}
    payload = {"channel": c["id"], "decision": "accept", "source": "host_tool_permission"}
    assert (await http.post("/decision", headers=headers, json=payload)).status_code == 200
    assert (await http.post("/decision", headers=headers, json={**payload, "source": "host_elicitation"})).status_code == 409
    assert (await http.post("/decision", headers=headers, json={**payload, "source": "model_says_yes"})).status_code == 409
    assert app.state.broker.db.execute("SELECT source FROM approval_decisions").fetchone()[0] == "host_tool_permission"
