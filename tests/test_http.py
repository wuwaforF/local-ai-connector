import asyncio
import json
from pathlib import Path
import pytest
import httpx
from local_ai_connector.server import create_app


@pytest.fixture
def app(tmp_path):
    (tmp_path/"server.json").write_text(json.dumps({"admin_token":"admin-secret","peers":{"a":"a-secret","b":"b-secret","c":"c-secret"}}))
    for peer in ("a", "b", "c"):
        (tmp_path/f"{peer}.json").write_text(json.dumps({"client":"generic"}))
    app=create_app(tmp_path)
    yield app
    app.state.broker.close()


async def test_auth_approval_and_content_visibility(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as http:
        assert (await http.post("/call",json={"action":"status"})).status_code==401
        assert (await http.get("/admin",headers={"Authorization":"Bearer a-secret"})).status_code==403
        assert (await http.post("/call",headers={"Authorization":"Bearer a-secret","Origin":"https://example.com"},json={"action":"status"})).status_code==403
        r=await http.post("/call",headers={"Authorization":"Bearer a-secret"},json={"action":"open","target":"b","body":"confidential question","key":"one"})
        assert r.status_code==200
        cid=r.json()["id"]
        b={"Authorization":"Bearer b-secret"}
        assert not (await http.post("/call",headers=b,json={"action":"status"})).json()["channels"]
        assert not (await http.post("/call",headers=b,json={"action":"receive","timeout":0})).json()["messages"]
        assert (await http.post("/call",headers=b,json={"action":"send","channel":cid,"kind":"question","body":"x","key":"early"})).status_code==409
        admin={"Authorization":"Bearer admin-secret"}
        assert (await http.get("/admin",headers=admin)).json()["channels"][0]["original"]=="confidential question"
        assert (await http.post("/admin",headers=admin,json={"action":"approve","channel":cid})).status_code==200
        result=(await http.post("/call",headers=b,json={"action":"receive","channel":cid,"timeout":0})).json()
        assert result["messages"][0]["body"]=="confidential question"
        assert (await http.post("/call",headers={"Authorization":"Bearer c-secret"},json={"action":"receive","channel":cid,"timeout":0})).status_code==403


@pytest.mark.parametrize("payload",[{},[],{"action":"receive","timeout":-1},{"action":"receive","after":True},{"action":"status","peer":"b"},{"action":"open","body":5},{"action":"send","kind":"unknown"}])
async def test_invalid_http_inputs(app,payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver",headers={"Authorization":"Bearer a-secret"}) as http:
        assert (await http.post("/call",json=payload)).status_code==409


async def test_host_boundary(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://untrusted.example") as http:
        assert (await http.get("/admin")).status_code==400


async def test_incidents_exclude_payload_and_other_peers(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as http:
        a={"Authorization":"Bearer a-secret"}
        response=await http.post("/call",headers=a,json={"action":"open","target":"missing","body":"private-original","key":"private-key"})
        assert response.json()["error"]=="invalid_target"
        own=(await http.post("/call",headers=a,json={"action":"status"})).json()["incidents"]
        assert len(own)==1 and own[0]["code"]=="invalid_target"
        assert "private" not in json.dumps(own)
        other=(await http.post("/call",headers={"Authorization":"Bearer b-secret"},json={"action":"status"})).json()
        assert other["incidents"]==[]
        admin=(await http.get("/admin",headers={"Authorization":"Bearer admin-secret"})).json()
        assert admin["incidents"]==own
