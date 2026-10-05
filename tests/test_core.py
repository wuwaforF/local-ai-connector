import asyncio
import pytest
from local_ai_connector.core import Broker, ConnectorError


@pytest.fixture
def broker(tmp_path):
    b=Broker(tmp_path/"state.sqlite")
    for p in ("codex","zcode","other"):
        b.register(p,p+"-token")
    yield b
    b.close()


async def test_conversation_approval_followup_and_idle(broker):
    b=broker
    now=[1000.0]
    b.clock=lambda:now[0]
    c=await b.open("codex","zcode","请确认求和所用的数字","req-1",idle_seconds=2)
    cid=c["id"]
    assert not (await b.receive("zcode",timeout=0))["messages"]
    assert not b.snapshot("zcode")["channels"]
    waiting=asyncio.create_task(b.receive("zcode",timeout=10))
    await asyncio.sleep(0)
    assert not waiting.done()
    await b.decide(cid,True)
    original=(await waiting)["messages"][0]
    assert original["body"]==c["original"]
    now[0]+=30
    assert b.snapshot()["channels"][0]["status"]=="active"
    follow=await b.send("zcode",cid,"question","要计算哪两个数？","follow-1",original["id"])
    got=await b.receive("codex",cid,timeout=0)
    assert got["messages"][0]["id"]==follow["id"]
    with pytest.raises(ConnectorError,match="未完成"):
        await b.finish("codex",cid)
    await b.send("codex",cid,"answer","17 和 25","numbers-1",follow["id"])
    reply=await b.send("zcode",cid,"answer","42","sum-1",original["id"])
    assert (await b.receive("codex",cid,got["cursor"],0))["messages"]==[reply]
    assert await b.send("zcode",cid,"answer","42","sum-1",original["id"])==reply
    assert not (await b.receive("codex",cid,reply["seq"],0))["messages"]
    await b.finish("codex",cid)
    now[0]+=3
    assert (await b.receive("codex",cid,reply["seq"],0))["state"]=="closed"


async def test_boundaries_and_dedup(broker):
    b=broker
    c=await b.open("codex","zcode","question","key")
    assert (await b.open("codex","zcode","question","key"))["id"]==c["id"]
    with pytest.raises(ConnectorError): await b.open("codex","other","changed","key")
    with pytest.raises(ConnectorError): await b.send("zcode",c["id"],"question","early","k")
    with pytest.raises(ConnectorError): await b.receive("other",c["id"],timeout=0)
    await b.decide(c["id"],True)
    original=(await b.receive("zcode",timeout=0))["messages"][0]
    with pytest.raises(ConnectorError): await b.send("codex",c["id"],"answer","self","k",original["id"])
    with pytest.raises(ConnectorError): await b.send("zcode",c["id"],"answer","bad","k","nonexistent")
    await b.send("zcode",c["id"],"answer","ok","k",original["id"])
    with pytest.raises(ConnectorError): await b.send("zcode",c["id"],"answer","changed","k",original["id"])
    with pytest.raises(ConnectorError): await b.send("zcode",c["id"],"answer","repeat","k2",original["id"])


async def test_revoke_wakes_waiter_and_cancel_does_not_lose_message(broker):
    b=broker
    c=await b.open("codex","zcode","q","key")
    task=asyncio.create_task(b.receive("codex",c["id"],timeout=30))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    task=asyncio.create_task(b.receive("codex",c["id"],timeout=30))
    await asyncio.sleep(0)
    await b.revoke(c["id"])
    assert (await task)["state"]=="revoked"
    with pytest.raises(ConnectorError): await b.decide(c["id"],True)


async def test_expiry_and_restart_replay(tmp_path):
    path=tmp_path/"db"
    b=Broker(path)
    b.register("codex","a"); b.register("zcode","b")
    c=await b.open("codex","zcode","q","key",ttl_seconds=10)
    await b.decide(c["id"],True)
    original=(await b.receive("zcode",timeout=0))["messages"][0]
    b.close()
    b=Broker(path)
    assert (await b.receive("zcode",timeout=0))["messages"][0]==original
    b.clock=lambda:c["expires"]+1
    assert (await b.receive("codex",c["id"],timeout=0))["state"]=="expired"
    assert not (await b.receive("zcode",timeout=0))["messages"]
    b.close()
