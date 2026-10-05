import json
import sys

import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from local_ai_connector.mcp_server import create_mcp


async def invoke(ctx, **payload):
    return payload


async def decide(ctx, channel, decision):
    raise AssertionError("Metadata tests must not submit decisions")


@pytest.mark.parametrize("requester", [False, True])
async def test_mcp_locale_is_per_instance_and_preserves_tool_contract(requester):
    options = {"decision": decide} if requester else {}
    async with Client(create_mcp(invoke, **options), mode="legacy") as chinese, Client(
        create_mcp(invoke, mcp_locale="en-US", **options), mode="legacy"
    ) as english:
        assert chinese.server_info.name == "本地 AI 连接器"
        assert english.server_info.name == "Local AI Connector"
        assert "委派" in chinese.instructions
        assert english.instructions.isascii()
        assert "connector_status" in english.instructions
        assert ("connector_delegate" if requester else "connector_receive") in english.instructions
        chinese_tools = (await chinese.list_tools()).tools
        english_tools = (await english.list_tools()).tools
        assert len(english_tools) == (8 if requester else 10)
        for old, new in zip(chinese_tools, english_tools, strict=True):
            assert old.description and not old.description.isascii()
            assert new.description and new.description.isascii()
            assert new.description != old.description
            assert old.model_dump(exclude={"description"}) == new.model_dump(exclude={"description"})
        assert (await chinese.list_tools()).tools == chinese_tools
        assert json.loads((await english.call_tool("connector_status", {})).content[0].text) == {"action": "status"}
        if not requester:
            args = {"channel": "channel-1", "message": "原样保留；Keep this exact.",
                    "message_key": "reply-1", "reply_to": "question-1"}
            result = json.loads((await english.call_tool("connector_send", args)).content[0].text)
            assert result == {"action": "send", "channel": args["channel"], "body": args["message"],
                              "key": args["message_key"], "kind": "answer", "reply_to": args["reply_to"]}


@pytest.mark.parametrize("locale, expected", [("zh-CN", "本地连接失败"), ("en-US", "Local connection failed")])
async def test_transport_error_uses_instance_locale(locale, expected):
    async def failed(ctx, **payload):
        raise httpx.ConnectError("synthetic connection failure")
    async with Client(create_mcp(failed, mcp_locale=locale), mode="legacy") as client:
        result = await client.call_tool("connector_status", {})
        assert result.is_error
        assert "transport_error:" in result.content[0].text
        assert expected in result.content[0].text


@pytest.mark.parametrize("locale, expected", [("zh-CN", "连接暂时不可用"), ("en-US", "Connection is temporarily unavailable")])
async def test_delegate_transport_error_uses_instance_locale(monkeypatch, locale, expected):
    async def failed(*args, **kwargs):
        raise httpx.ConnectError("synthetic connection failure")
    monkeypatch.setattr("local_ai_connector.delegation.delegate", failed)
    async with Client(create_mcp(invoke, decision=decide, mcp_locale=locale), mode="legacy") as client:
        result = await client.call_tool("connector_delegate", {"conversation_mode": "existing", "target": "worker", "message": "Task", "request_key": "same-key"})
        assert result.is_error
        assert "transport_error:" in result.content[0].text
        assert expected in result.content[0].text
        assert "request_key" in result.content[0].text


@pytest.mark.parametrize("locale", [None, "fr-FR", "", 42, ["en-US"], True])
def test_unknown_mcp_locale_is_rejected(locale):
    with pytest.raises(ValueError, match="mcp_locale"):
        create_mcp(invoke, mcp_locale=locale)


@pytest.mark.parametrize("locale, expected_name", [(None, "本地 AI 连接器"), ("zh-CN", "本地 AI 连接器"), ("en-US", "Local AI Connector")])
async def test_stdio_reads_endpoint_locale_without_connecting_to_broker(tmp_path, locale, expected_name):
    config = {"url": "http://127.0.0.1:1", "token": "synthetic", "peer": "worker", "client": "generic"}
    if locale is not None:
        config["mcp_locale"] = locale
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "local_ai_connector.cli", "mcp", "--config", str(path)])
    async with Client(parameters, mode="legacy") as client:
        assert client.server_info.name == expected_name
        assert client.instructions.isascii() == (locale == "en-US")
        assert all(tool.description.isascii() == (locale == "en-US") for tool in (await client.list_tools()).tools)
    assert path.read_bytes() == before
