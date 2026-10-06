"""Antigravity worker identity through the installed entry, with chats told apart by request metadata.

Antigravity runs one MCP process for all chats and adds the calling conversation to each tool
call's `_meta` (`antigravity.google/conversation_id`, observed on 2.19.1 and cross-checked
against the documented hook payload). Here two simulated chats share one MCP process, as
they do in the host. Approvals are simulated; this is not real-desktop acceptance.
"""
import json

import tomlkit  # noqa: F401  (keeps the same dependency surface as the startup test)
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import ElicitResult

from local_ai_connector import service
from local_ai_connector.hosts import HOSTS, HostEnv
from local_ai_connector.install import profile_dir
from test_startup_e2e import body, installed, run_cli  # noqa: F401  (fixture)

# As observed: Antigravity also sends the tool call's progress token, which its forms are routed by.
CHAT_A = {"antigravity.google/conversation_id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "progress_token": "trajectory-a:10"}
CHAT_B = {"antigravity.google/conversation_id": "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb", "progress_token": "trajectory-b:4"}


async def test_antigravity_chats_are_identified_per_call_and_isolated(installed):
    environ, ctx = installed
    env = HostEnv(ctx.home, environ)
    run_cli(environ, "setup", "antigravity", "--no-start")
    run_cli(environ, "setup", "claude", "--no-start")
    entry = json.loads(HOSTS["antigravity"].config_path(env).read_text())["mcpServers"]["local_ai_connector"]
    claude_entry = json.loads(HOSTS["claude"].config_path(env).read_text())["mcpServers"]["local_ai_connector"]
    forms = []

    async def user(ctx_, params):
        assert (params.meta or {}).get("progress_token") in ("trajectory-a:10", "trajectory-b:4")  # forms routed by the call's token
        forms.append(params.message)
        return ElicitResult(action="accept", content={})

    antigravity = StdioServerParameters(command=entry["command"], args=entry["args"], env=environ)
    claude = StdioServerParameters(command=claude_entry["command"], args=claude_entry["args"],
                                   env={**environ, "CLAUDE_CODE_SESSION_ID": "initiator-1"})
    task = {"target": "antigravity", "message": "What is 6*7?", "conversation_mode": "existing"}
    # One MCP process serves both Antigravity chats, as in the host.
    async with Client(antigravity, elicitation_callback=user) as app, Client(claude) as initiator:
        missing = await app.call_tool("connector_bind_this_chat", {"label": "Worker A", "request_key": "b0"})
        assert missing.is_error and "missing_session_identity" in missing.content[0].text
        bound = body(await app.call_tool("connector_bind_this_chat", {"label": "Worker A", "request_key": "bA"},
                                         meta=CHAT_A))
        assert bound["revision"] == 1 and "antigravity:aaaaaaaa" in forms[-1]

        first = body(await initiator.call_tool("connector_delegate", {**task, "request_key": "t1",
                                                                      "target_chat": "Worker A", "binding_revision": 1}))
        channel = first["channel"]
        assert body(await app.call_tool("connector_receive", {"timeout": 0}, meta=CHAT_B))["messages"] == []
        for call in (app.call_tool("connector_receive", {"channel": channel, "timeout": 0}, meta=CHAT_B),
                     app.call_tool("connector_receive", {"channel": channel, "timeout": 0})):
            refused = await call
            assert refused.is_error and "wrong_session" in refused.content[0].text
        question = body(await app.call_tool("connector_receive", {"timeout": 10}, meta=CHAT_A))["messages"][0]
        forged = await app.call_tool("connector_send", {"channel": channel, "message": "41", "message_key": "x",
                                                        "reply_to": question["id"]}, meta=CHAT_B)
        assert forged.is_error and "wrong_session" in forged.content[0].text
        body(await app.call_tool("connector_send", {"channel": channel, "message": "42", "message_key": "a1",
                                                    "reply_to": question["id"]}, meta=CHAT_A))
        answer = body(await initiator.call_tool("connector_receive", {"channel": channel, "timeout": 10}))
        assert [m["body"] for m in answer["messages"]] == ["42"]

        # Approved while A is bound, then B binds: the approved task stays with A.
        second = body(await initiator.call_tool("connector_delegate", {**task, "request_key": "t2",
                                                                       "target_chat": "Worker A", "binding_revision": 1}))
        assert body(await app.call_tool("connector_bind_this_chat", {"label": "Worker B", "request_key": "bB"},
                                        meta=CHAT_B))["revision"] == 2
        assert second["channel"] not in {m["channel"] for m in body(await app.call_tool(
            "connector_receive", {"timeout": 0}, meta=CHAT_B))["messages"]}

        # A restarted service keeps bindings and pins; identity is still read per call.
        assert service.stop(profile_dir("default", ctx)) == "stopped"
        assert [m["channel"] for m in body(await app.call_tool(
            "connector_receive", {"timeout": 10, "after": question["seq"]}, meta=CHAT_A))["messages"]] == [second["channel"]]
        third = body(await initiator.call_tool("connector_delegate", {**task, "request_key": "t3",
                                                                      "target_chat": "Worker B", "binding_revision": 2}))
        assert [m["channel"] for m in body(await app.call_tool(
            "connector_receive", {"timeout": 10}, meta=CHAT_B))["messages"]] == [third["channel"]]
