import asyncio
from contextlib import asynccontextmanager
import json
import socket
import sys

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
import pytest
import uvicorn

from local_ai_connector.server import create_app


@asynccontextmanager
async def run_server(tmp_path):
    peers = {"alpha": "test-a", "beta": "test-b", "gamma": "test-c", "native": "test-native"}
    (tmp_path/"server.json").write_text(json.dumps({"admin_token":"test-admin","peers":peers}))
    sock=socket.socket()
    sock.bind(("127.0.0.1",0))
    sock.listen()
    address=f"http://127.0.0.1:{sock.getsockname()[1]}"
    paths=[]
    for name, token in peers.items():
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps({"url": address, "token": token, "peer": name, "client": "codex" if name == "native" else "generic"}))
        paths.append(path)
    app=create_app(tmp_path)
    server=uvicorn.Server(uvicorn.Config(app,log_level="error",timeout_graceful_shutdown=1))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done(): await task
                await asyncio.sleep(0.01)
        yield address,paths
    finally:
        server.should_exit=True
        await task


@pytest.fixture
async def running(tmp_path):
    async with run_server(tmp_path) as state:
        yield state


@asynccontextmanager
async def http_mcp(address, token, *, mode="auto"):
    async with create_mcp_http_client(headers={"Authorization": "Bearer " + token}) as http:
        async with Client(streamable_http_client(address + "/mcp", http_client=http), mode=mode) as client:
            yield client


async def test_actual_stdio_wait_approval_reply_and_errors(running):
    address,paths=running
    a,b=[Client(StdioServerParameters(command=sys.executable,args=["-m","local_ai_connector.cli","mcp","--config",str(p)])) for p in paths[:2]]
    async with a,b,httpx.AsyncClient(base_url=address,headers={"Authorization":"Bearer test-admin"},trust_env=False) as admin:
        assert len((await a.list_tools()).tools)==10
        r=await a.call_tool("connector_request_help",{"conversation_mode": "existing", "target":"beta","message":"需要校对两个整数的和","request_key":"integration-1"})
        assert not r.is_error
        c=json.loads(r.content[0].text)
        wait=asyncio.create_task(b.call_tool("connector_receive",{"timeout":10}))
        await asyncio.sleep(0.1)
        assert not wait.done()
        response=await admin.post("/admin",json={"action":"approve","channel":c["id"]})
        assert response.status_code==200
        question=json.loads((await wait).content[0].text)["messages"][0]
        error=await b.call_tool("connector_send",{"channel":c["id"],"message":"42","message_key":"bad-reply","reply_to":"wrong-id"})
        assert error.is_error
        assert "wrong_reply" in error.content[0].text
        r=await b.call_tool("connector_send",{"channel":c["id"],"message":"42","message_key":"answer-1","reply_to":question["id"]})
        assert not r.is_error
        result=json.loads((await a.call_tool("connector_receive",{"channel":c["id"],"timeout":0})).content[0].text)
        assert result["messages"][0]["body"]=="42"
        assert not (await a.call_tool("connector_finish",{"channel":c["id"]})).is_error
        waiting=asyncio.create_task(a.call_tool("connector_receive",{"channel":c["id"],"after":result["cursor"],"timeout":300}))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):await waiting


async def test_http_disconnect_replay_dedup_and_revoke(running):
    address,_=running
    a_headers={"Authorization":"Bearer test-a"}
    async with httpx.AsyncClient(base_url=address,headers=a_headers,trust_env=False) as a, httpx.AsyncClient(base_url=address,headers={"Authorization":"Bearer test-b"},trust_env=False) as b, httpx.AsyncClient(base_url=address,headers={"Authorization":"Bearer test-admin"},trust_env=False) as admin:
        opened=await a.post("/call",json={"action":"open","target":"beta","body":"恢复测试","key":"reconnect"})
        opened.raise_for_status()
        cid=opened.json()["id"]
        (await admin.post("/admin",json={"action":"approve","channel":cid})).raise_for_status()
        original=(await b.post("/call",json={"action":"receive","channel":cid,"timeout":0})).json()["messages"][0]
        waiting=asyncio.create_task(a.post("/call",json={"action":"receive","channel":cid,"timeout":300},timeout=310))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):await waiting
        await a.aclose()
        payload={"action":"send","channel":cid,"kind":"answer","body":"已恢复","key":"reconnect-answer","reply_to":original["id"]}
        first=await b.post("/call",json=payload)
        first.raise_for_status()
        assert (await b.post("/call",json=payload)).json()==first.json()
        async with httpx.AsyncClient(base_url=address,headers=a_headers,trust_env=False) as resumed:
            result=(await resumed.post("/call",json={"action":"receive","channel":cid,"after":0,"timeout":0})).json()
            assert [m["body"] for m in result["messages"]]==["已恢复"]
            cursor=result["cursor"]
            waiting=asyncio.create_task(resumed.post("/call",json={"action":"receive","channel":cid,"after":cursor,"timeout":300},timeout=310))
            await asyncio.sleep(0.1)
            assert not waiting.done()
            (await admin.post("/admin",json={"action":"revoke","channel":cid})).raise_for_status()
            response=await asyncio.wait_for(waiting,2)
            assert response.json()=={"state":"revoked","messages":[],"cursor":cursor}
            rejected=await b.post("/call",json={**payload,"key":"after-revoke"})
            assert rejected.status_code==409 and rejected.json()["error"]=="channel_unavailable"


async def test_actual_stdio_expiry_wakes_waiter(running):
    address,paths=running
    a=Client(StdioServerParameters(command=sys.executable,args=["-m","local_ai_connector.cli","mcp","--config",str(paths[0])]))
    async with a,httpx.AsyncClient(base_url=address,headers={"Authorization":"Bearer test-admin"},trust_env=False) as admin:
        opened=await a.call_tool("connector_request_help",{"conversation_mode": "existing", "target":"beta","message":"到期唤醒测试","request_key":"expiry","ttl_seconds":10})
        assert not opened.is_error
        cid=json.loads(opened.content[0].text)["id"]
        (await admin.post("/admin",json={"action":"approve","channel":cid})).raise_for_status()
        waiting=asyncio.create_task(a.call_tool("connector_receive",{"channel":cid,"timeout":30}))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        result=await asyncio.wait_for(waiting,15)
        assert not result.is_error
        assert json.loads(result.content[0].text)["state"]=="expired"
        rejected=await a.call_tool("connector_send",{"channel":cid,"message":"已到期的追问","message_key":"expired-send","kind":"question"})
        assert rejected.is_error and "channel_unavailable" in rejected.content[0].text


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_streamable_http_stdio_roundtrip_and_isolation(running, mode):
    address, paths = running
    stdio = Client(StdioServerParameters(command=sys.executable, args=["-m", "local_ai_connector.cli", "mcp", "--config", str(paths[0])]))
    async with stdio as a, http_mcp(address, "test-b", mode=mode) as b, http_mcp(address, "test-c", mode=mode) as c, httpx.AsyncClient(base_url=address, headers={"Authorization": "Bearer test-admin"}, trust_env=False) as admin:
        stdio_tools = (await a.list_tools()).tools
        http_tools = (await b.list_tools()).tools
        assert [t.model_dump() for t in stdio_tools] == [t.model_dump() for t in http_tools]
        assert next(t for t in http_tools if t.name == "connector_receive").input_schema["properties"]["timeout"]["default"] == 20
        opened = await a.call_tool("connector_request_help", {"conversation_mode": "existing", "target": "beta", "message": "17 + 25", "request_key": "mixed"})
        assert not opened.is_error
        cid = json.loads(opened.content[0].text)["id"]
        before = json.loads((await b.call_tool("connector_receive", {"timeout": 0})).content[0].text)
        assert before["messages"] == []
        waiting = asyncio.create_task(b.call_tool("connector_receive", {"timeout": 10}))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        (await admin.post("/admin", json={"action": "approve", "channel": cid})).raise_for_status()
        received = await waiting
        assert not received.is_error
        question = json.loads(received.content[0].text)["messages"][0]
        forbidden = await c.call_tool("connector_receive", {"channel": cid, "timeout": 0}, meta={"peer": "beta"})
        assert forbidden.is_error and "forbidden" in forbidden.content[0].text
        assert json.loads((await c.call_tool("connector_status")).content[0].text)["channels"] == []
        reply = {"channel": cid, "message": "42", "message_key": "answer", "reply_to": question["id"]}
        first = await b.call_tool("connector_send", reply)
        assert not first.is_error
        assert (await b.call_tool("connector_send", reply)).content == first.content
        result = json.loads((await a.call_tool("connector_receive", {"channel": cid, "timeout": 0})).content[0].text)
        assert [m["body"] for m in result["messages"]] == ["42"]
        assert not (await a.call_tool("connector_finish", {"channel": cid})).is_error
        invalid = await b.call_tool("connector_receive", {"timeout": -1})
        assert invalid.is_error and "invalid_request" in invalid.content[0].text


async def test_generic_stdio_restart_preserves_endpoint_and_pending_messages(running):
    address, paths = running
    config = json.loads(paths[0].read_text())
    config["bound_session"] = "generic:old-process-uuid"
    paths[0].write_text(json.dumps(config))
    before = paths[0].read_bytes()
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "local_ai_connector.cli", "mcp", "--config", str(paths[0])])
    async with Client(parameters) as first:
        opened = await first.call_tool("connector_request_help", {"conversation_mode": "existing", "target": "beta", "message": "重启后接收", "request_key": "restart"})
        assert not opened.is_error
        cid = json.loads(opened.content[0].text)["id"]
    async with http_mcp(address, "test-b") as b, httpx.AsyncClient(base_url=address, headers={"Authorization": "Bearer test-admin"}, trust_env=False) as admin:
        (await admin.post("/admin", json={"action": "approve", "channel": cid})).raise_for_status()
        question = json.loads((await b.call_tool("connector_receive", {"timeout": 0})).content[0].text)["messages"][0]
        assert not (await b.call_tool("connector_send", {"channel": cid, "message": "重启不串端点", "message_key": "restart-reply", "reply_to": question["id"]})).is_error
    async with Client(parameters) as restarted:
        result = await restarted.call_tool("connector_receive", {"channel": cid, "timeout": 0})
        assert not result.is_error
        assert json.loads(result.content[0].text)["messages"][0]["body"] == "重启不串端点"
    assert paths[0].read_bytes() == before


async def test_http_reconnect_and_broker_restart_replay(tmp_path):
    async with run_server(tmp_path) as (address, _):
        async with http_mcp(address, "test-a") as a, httpx.AsyncClient(base_url=address, headers={"Authorization": "Bearer test-admin"}, trust_env=False) as admin:
            opened = await a.call_tool("connector_request_help", {"conversation_mode": "existing", "target": "beta", "message": "持久消息", "request_key": "broker-restart"})
            assert not opened.is_error
            cid = json.loads(opened.content[0].text)["id"]
            (await admin.post("/admin", json={"action": "approve", "channel": cid})).raise_for_status()
    async with run_server(tmp_path) as (address, _):
        async with http_mcp(address, "test-b") as b:
            result = await b.call_tool("connector_receive", {"channel": cid, "timeout": 0})
            assert not result.is_error
            received = json.loads(result.content[0].text)
            assert [m["body"] for m in received["messages"]] == ["持久消息"]
        async with http_mcp(address, "test-b") as reconnected:
            result = await reconnected.call_tool("connector_receive", {"channel": cid, "after": received["cursor"], "timeout": 1})
            assert not result.is_error
            assert json.loads(result.content[0].text) == {"state": "waiting", "messages": [], "cursor": received["cursor"]}


async def test_http_mcp_auth_boundary(running):
    address, _ = running
    async with httpx.AsyncClient(base_url=address, trust_env=False) as http:
        for method in ("GET", "POST", "DELETE"):
            for token in (None, "invalid", "test-admin"):
                headers = {} if token is None else {"Authorization": "Bearer " + token}
                assert (await http.request(method, "/mcp", headers=headers)).status_code == 401
            for origin in ("https://example.com", ""):
                blocked = await http.request(method, "/mcp", headers={"Authorization": "Bearer test-a", "Origin": origin})
                assert blocked.status_code == 403
            native = await http.request(method, "/mcp", headers={"Authorization": "Bearer test-native"})
            assert native.status_code == 403 and native.json()["error"] == "native_stdio_required"
        assert (await http.post("/mcp", headers={"Authorization": "Bearer test-a", "Host": "untrusted.example"})).status_code == 400


async def test_native_stdio_binding_survives_restart_and_rejects_other_task(running):
    _, paths = running
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "local_ai_connector.cli", "mcp", "--config", str(paths[3])])
    meta = {"x-codex-turn-metadata": {"thread_id": "native-task-a"}}
    async with Client(parameters) as first:
        missing = await first.call_tool("connector_status")
        assert missing.is_error and "missing_session_identity" in missing.content[0].text
        assert not (await first.call_tool("connector_status", meta=meta)).is_error
    async with Client(parameters) as restarted:
        assert not (await restarted.call_tool("connector_status", meta=meta)).is_error
        other = await restarted.call_tool("connector_status", meta={"x-codex-turn-metadata": {"thread_id": "native-task-b"}})
        assert other.is_error and "session_conflict" in other.content[0].text
    assert json.loads(paths[3].read_text())["bound_session"] == "codex:native-task-a"


async def test_http_wait_cancel_replay_and_revoke(running):
    address, _ = running
    async with http_mcp(address, "test-a") as a, http_mcp(address, "test-b") as b, httpx.AsyncClient(base_url=address, headers={"Authorization": "Bearer test-admin"}, trust_env=False) as admin:
        opened = await a.call_tool("connector_request_help", {"conversation_mode": "existing", "target": "beta", "message": "取消不丢消息", "request_key": "cancel-mcp"})
        cid = json.loads(opened.content[0].text)["id"]
        waiting = asyncio.create_task(b.call_tool("connector_receive", {"timeout": 10}))
        await asyncio.sleep(0.1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        (await admin.post("/admin", json={"action": "approve", "channel": cid})).raise_for_status()
        received = await b.call_tool("connector_receive", {"timeout": 0})
        assert not received.is_error
        result = json.loads(received.content[0].text)
        assert [m["body"] for m in result["messages"]] == ["取消不丢消息"]
        waiting = asyncio.create_task(b.call_tool("connector_receive", {"channel": cid, "after": result["cursor"], "timeout": 10}))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        (await admin.post("/admin", json={"action": "revoke", "channel": cid})).raise_for_status()
        revoked = await asyncio.wait_for(waiting, 2)
        assert not revoked.is_error
        assert json.loads(revoked.content[0].text) == {"state": "revoked", "messages": [], "cursor": result["cursor"]}
