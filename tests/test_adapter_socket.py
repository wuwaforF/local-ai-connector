import asyncio
import json
from pathlib import Path
import stat
import sys
import tempfile

import httpx
import pytest

from local_ai_connector.adapter_socket import SocketAdapter, serve
from local_ai_connector.server import create_app
from local_ai_connector.wakeup import AdapterError, load_config

pytestmark = pytest.mark.posix  # Unix sockets, owner/mode bits or fcntl

BRIDGE = '''
import json, sys
r = json.load(sys.stdin)
op = r['op']
print(json.dumps({'ok':True, **({'target':r['target']} if op=='confirm' else
    {'state':'idle'} if op=='status' else {'accepted':True,'host_ref':r['dispatch_id']} if op=='send'
    else {'result':'unknown'})}))
'''


@pytest.fixture
def socket_path():
    # macOS limits Unix socket paths to 104 bytes; pytest's default temp path can exceed it.
    with tempfile.TemporaryDirectory(prefix="lac-", dir="/tmp") as directory:
        yield Path(directory) / "bridge.sock"


async def test_bridge_contract_and_unavailability_are_separate_from_service(socket_path):
    task = asyncio.create_task(serve(socket_path, [sys.executable, "-c", BRIDGE], 5))
    try:
        async with asyncio.timeout(5):
            while not socket_path.exists():
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        adapter = SocketAdapter(str(socket_path), 5)
        target = {"session": "arbitrary-host-session"}
        assert await adapter.confirm(target) == target
        assert await adapter.status(target) == "idle"
        assert (await adapter.send(target, "dispatch-1", "wake"))["host_ref"] == "dispatch-1"
        assert await adapter.reconcile(target, "dispatch-1") == "unknown"
        duplicate = asyncio.create_task(serve(socket_path, [sys.executable, "-c", BRIDGE], 5))
        with pytest.raises(BlockingIOError):
            await duplicate
        assert await adapter.status(target) == "idle"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not socket_path.exists()
    with pytest.raises(AdapterError) as error:
        await adapter.send(target, "dispatch-2", "wake")
    assert error.value.kind == "unavailable" and not error.value.retryable


async def test_socket_disconnect_after_submission_remains_ambiguous(socket_path):
    calls = []
    async def disconnect(reader, writer):
        calls.append(json.loads(await reader.readline()))
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_unix_server(disconnect, path=str(socket_path))
    async with server:
        adapter = SocketAdapter(str(socket_path), 2)
        with pytest.raises(AdapterError) as error:
            await adapter.send({"session": "test"}, "dispatch", "wake")
        assert error.value.kind == "ambiguous"
        assert len(calls) == 1


async def test_missing_host_bridge_does_not_disable_generic_mcp(tmp_path, socket_path):
    config = {"url": "http://127.0.0.1", "admin_token": "test-admin", "peers": {"a": "test-a", "b": "test-b"}}
    (tmp_path / "server.json").write_text(json.dumps(config))
    for name, token in config["peers"].items():
        (tmp_path / f"{name}.json").write_text(json.dumps({"client": "generic", "token": token}))
    wake = {"enabled": True, "settle_seconds": 0, "poll_seconds": 0.05,
            "bindings": {"b": {"adapter": "socket", "socket": str(socket_path), "target": {"session": "test"}}}}
    (tmp_path / "wakeup.json").write_text(json.dumps(wake))
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=config["url"]) as http:
            request = await http.post("/call", headers={"Authorization": "Bearer test-a"},
                                      json={"action": "open", "target": "b", "body": "question", "key": "q"})
            cid = request.json()["id"]
            await http.post("/admin", headers={"Authorization": "Bearer test-admin"}, json={"action": "approve", "channel": cid})
            async with asyncio.timeout(3):
                while not app.state.broker.snapshot()["incidents"]:
                    await asyncio.sleep(0.01)
            assert app.state.broker.snapshot()["incidents"][0]["code"] == "wake_host_unavailable"
            received = await http.post("/call", headers={"Authorization": "Bearer test-b"}, json={"action": "receive", "timeout": 0})
            assert received.status_code == 200
            question = received.json()["messages"][0]
            answer = await http.post("/call", headers={"Authorization": "Bearer test-b"},
                                    json={"action": "send", "channel": cid, "kind": "answer", "body": "reply", "key": "r", "reply_to": question["id"]})
            assert answer.status_code == 200
            received = await http.post("/call", headers={"Authorization": "Bearer test-a"}, json={"action": "receive", "channel": cid, "timeout": 0})
            assert received.json()["messages"][0]["body"] == "reply"


@pytest.mark.parametrize("extra", [{"socket": "relative.sock"}, {"command": ["unexpected"]}])
def test_socket_config_is_explicit(tmp_path, socket_path, extra):
    spec = {"adapter": "socket", "socket": str(socket_path), "target": {"session": "test"}, **extra}
    (tmp_path / "wakeup.json").write_text(json.dumps({"bindings": {"worker": spec}}))
    with pytest.raises(ValueError):
        load_config(tmp_path, {"worker"})


@pytest.mark.parametrize('enabled', [False, True])
async def test_native_wire_operations_require_explicit_sidecar_enablement(socket_path, enabled):
    native_bridge = '''
import json,sys
r=json.load(sys.stdin)
if r['op']=='create':
    answer={'ok':True,'target':{**r['target'],'conversation_id':'22222222-2222-4222-8222-222222222222'}}
else:
    answer={'ok':True,'archived':True,'target':r['target']}
print(json.dumps(answer))
'''
    server = asyncio.create_task(serve(socket_path, [sys.executable, '-c', native_bridge], 5, native_conversations=enabled))
    try:
        async with asyncio.timeout(3):
            while not socket_path.exists():
                if server.done(): await server
                await asyncio.sleep(.01)
        adapter = SocketAdapter(str(socket_path), 3)
        args = {'target': {'project_id': 'project', 'workspace_uri': 'file:///test'},
                'dispatch_id': 'dispatch', 'text': 'wake', 'expires_at': 1000, 'title': 'test'}
        if not enabled:
            with pytest.raises(AdapterError): await adapter._call('create', **args)
        else:
            created = await adapter._call('create', **args)
            archived = await adapter._call('archive', target=created['target'], expires_at=1000)
            assert archived['archived'] is True and archived['target'] == created['target']
    finally:
        server.cancel()
        with pytest.raises(asyncio.CancelledError): await server
