import asyncio
import json

import httpx
import pytest
from mcp import Client
from mcp_types import ElicitResult

from local_ai_connector.core import ConnectorError
from local_ai_connector.mcp_server import create_mcp
from local_ai_connector.server import create_app


@pytest.fixture
def workflow_app(tmp_path):
    (tmp_path / "server.json").write_text(json.dumps({"admin_token": "test-admin",
        "peers": {"owner": "test-owner", "builder": "test-builder", "reviewer": "test-reviewer"},
        "approval_tokens": {"owner": "test-approval"}}))
    for peer in ("owner", "builder", "reviewer"):
        (tmp_path / f"{peer}.json").write_text('{"client":"generic"}')
    app = create_app(tmp_path)
    yield app
    app.state.broker.close()


async def test_workflow_mcp_uses_existing_host_confirmation_and_http_acl(workflow_app):
    broker = workflow_app.state.broker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=workflow_app), base_url="http://testserver") as http:
        async def invoke(ctx, **payload):
            response = await http.post("/call", json=payload, headers={"Authorization": "Bearer test-owner"})
            result = response.json()
            if response.status_code != 200:
                raise ConnectorError(result["error"], result["message"])
            return result

        async def decide(ctx, channel, decision):
            response = await http.post("/decision", json={"channel": channel, "decision": decision},
                                       headers={"Authorization": "Bearer test-approval"})
            assert response.status_code == 200
            return response.json()

        prompts = []
        async def confirmation(ctx, params):
            prompts.append(params.message)
            return ElicitResult(action="accept", content={})

        async with Client(create_mcp(invoke, decision=decide, mcp_locale="en-US"),
                          mode="legacy", elicitation_callback=confirmation) as owner:
            started = await owner.call_tool("connector_workflow_start", {"plan": "Produce the text 893.",
                "builder": "builder", "reviewer": "reviewer", "request_key": "pipeline-transport"})
            assert not started.is_error
            run = json.loads(started.content[0].text)
            assert prompts == [] and (await broker.receive("builder", timeout=0))["messages"] == []
            other = await http.post("/call", json={"action": "workflow_status", "workflow_id": run["id"]},
                                    headers={"Authorization": "Bearer test-reviewer"})
            assert other.status_code == 403 and "Produce" not in other.text
            for role in ("build", "review"):
                stage = run["stages"][-1]
                async def answer_stage():
                    incoming = await broker.receive(stage["target"], timeout=3)
                    question = incoming["messages"][0]
                    common = {"schema_version": 1, "run_id": run["id"], "stage_id": stage["id"]}
                    if role == "build":
                        output = {**common, "candidate_content": "893", "verification": "Arithmetic checked by fixture worker"}
                    else:
                        candidate = run["stages"][0]["output"]
                        output = {**common, "build_stage_id": candidate["build_stage_id"],
                            "candidate_sha256": candidate["candidate_sha256"], "verdict": "accept", "findings": "Exact stored candidate checked"}
                    await broker.send(stage["target"], question["channel"], "answer", json.dumps(output), stage["id"], question["id"])
                worker = asyncio.create_task(answer_stage())
                delegated = await owner.call_tool("connector_delegate", {**run["delegation"], "timeout_seconds": 3})
                await worker
                assert not delegated.is_error
                resumed = await owner.call_tool("connector_workflow_resume", {"workflow_id": run["id"], "expected_version": run["version"]})
                assert not resumed.is_error
                run = json.loads(resumed.content[0].text)
                if role == "build":
                    assert run["stages"][-1]["channel_observation"]["approved_at"] is None
                    assert (await broker.receive("reviewer", timeout=0))["messages"] == []
            assert run["state"] == "accepted" and len(prompts) == 2
            assert [row[0] for row in broker.db.execute("SELECT decision FROM approval_decisions")] == ["accept", "accept"]
            assert run["stages"][0]["output"]["candidate_content"] == "893"


@pytest.mark.parametrize("extra", [{"approved": True}, {"expected_version": True}, {"max_revisions": 4}, {"conversation_mode": "new"}])
async def test_workflow_http_rejects_unscoped_or_invalid_inputs(workflow_app, extra):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=workflow_app), base_url="http://testserver",
                               headers={"Authorization": "Bearer test-owner"}) as http:
        payload = {"action": "workflow_start", "body": "plan", "builder": "builder", "reviewer": "reviewer", "key": "invalid", **extra}
        response = await http.post("/call", json=payload)
        assert response.status_code == 409
        assert workflow_app.state.broker.db.execute("SELECT count(*) FROM workflow_runs").fetchone()[0] == 0
