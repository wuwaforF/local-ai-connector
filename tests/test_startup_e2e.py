"""Cross-platform startup: the installed entries really launch, bind, approve, route and answer.

Runs on every CI platform with an isolated home. The service is started on demand as a real
detached process; MCP clients use exactly the command lines setup wrote into the host files.
Desktop hosts are simulated: Codex chats by per-call thread metadata, a Claude chat by its
session environment variable, approvals by the client's form callback.
"""
import json
import os
import subprocess
import sys

import pytest
import tomlkit
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import ElicitResult

from hostfakes import isolated_environ
from local_ai_connector import service
from local_ai_connector.hosts import HOSTS, HostEnv
from local_ai_connector.install import Context, profile_dir

CHAT_A = {"x-codex-turn-metadata": {"thread_id": "thread-a"}}
CHAT_B = {"x-codex-turn-metadata": {"thread_id": "thread-b"}}


def run_cli(environ, *args):
    result = subprocess.run([sys.executable, "-m", "local_ai_connector.cli", *args], env=environ,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def body(result):
    assert not result.is_error, result.content[0].text if result.content else result
    return json.loads(result.content[0].text)


@pytest.fixture
def installed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    environ = isolated_environ(home)
    ctx = Context(home=home, environ=environ)
    try:
        yield environ, ctx
    finally:
        data = profile_dir("default", ctx)
        if (data / "server.json").exists():
            service.stop(data)


async def test_install_bind_approve_route_answer_decline_restart_and_reinstall(installed):
    environ, ctx = installed
    env = HostEnv(ctx.home, environ)
    first = run_cli(environ, "setup", "codex")
    assert first["service"] == "started"
    assert run_cli(environ, "setup", "claude", "--no-start")["changed"] is True
    codex_entry = tomlkit.parse(HOSTS["codex"].config_path(env).read_text())["mcp_servers"]["local_ai_connector"]
    claude_entry = json.loads(HOSTS["claude"].config_path(env).read_text())["mcpServers"]["local_ai_connector"]
    codex = StdioServerParameters(command=codex_entry["command"], args=list(codex_entry["args"]), env=environ)
    claude = StdioServerParameters(command=claude_entry["command"], args=claude_entry["args"],
                                   env={**environ, "CLAUDE_CODE_SESSION_ID": "session-1"})
    prompts = []

    async def codex_user(ctx_, params):
        prompts.append(params.message)
        # Approve binding this chat; decline the task this chat starts later.
        return ElicitResult(action="accept", content={}) if params.message.startswith("Bind this chat") \
            else ElicitResult(action="decline")

    async with Client(codex, elicitation_callback=codex_user) as codex_app, Client(claude) as claude_chat:
        # Natural-language binding from inside the worker chat; the host supplies the identity.
        bound = body(await codex_app.call_tool("connector_bind_this_chat", {"label": "Reviewer", "request_key": "b1"},
                                               meta=CHAT_A))
        assert bound["state"] == "approved" and bound["revision"] == 1 and "codex:thread-a" in prompts[0]
        workers = {w["id"]: w for w in body(await claude_chat.call_tool("connector_status"))["workers"]}
        assert workers["codex"]["binding"]["label"] == "Reviewer"

        # Claude's native permission prompt shows only tool arguments, so they must name the bound chat.
        task = {"target": "codex", "message": "What is 17+25?", "request_key": "t1", "conversation_mode": "existing"}
        missing = await claude_chat.call_tool("connector_delegate", task)
        assert missing.is_error and "target_chat_required" in missing.content[0].text
        started = body(await claude_chat.call_tool("connector_delegate",
                                                   {**task, "target_chat": "Reviewer", "binding_revision": 1}))
        assert started["state"] == "active" and started["target_chat"]["pinned"] is True
        channel = started["channel"]

        # Another chat on the same Codex endpoint sees nothing and is refused.
        assert body(await codex_app.call_tool("connector_receive", {"timeout": 0}, meta=CHAT_B))["messages"] == []
        wrong = await codex_app.call_tool("connector_receive", {"channel": channel, "timeout": 0}, meta=CHAT_B)
        assert wrong.is_error and "wrong_session" in wrong.content[0].text

        received = body(await codex_app.call_tool("connector_receive", {"timeout": 10}, meta=CHAT_A))
        question = received["messages"][0]
        assert question["channel"] == channel and question["body"] == "What is 17+25?"
        body(await codex_app.call_tool("connector_send", {"channel": channel, "message": "42", "message_key": "a1",
                                                          "reply_to": question["id"]}, meta=CHAT_A))
        answer = body(await claude_chat.call_tool("connector_receive", {"channel": channel, "timeout": 10}))
        assert [m["body"] for m in answer["messages"]] == ["42"]

        # Decline in the initiating chat: the Claude chat binds itself, Codex starts a task, the user declines.
        assert body(await claude_chat.call_tool("connector_bind_this_chat",
                                                {"label": "Claude worker", "request_key": "b2"}))["state"] == "approved"
        declined = body(await codex_app.call_tool(
            "connector_delegate", {"target": "claude", "message": "Summarise this.", "request_key": "t2",
                                   "conversation_mode": "existing"}, meta=CHAT_A))
        assert declined["state"] == "denied"
        inbox = body(await claude_chat.call_tool("connector_receive", {"timeout": 0}))["messages"]
        assert declined["channel"] not in {m["channel"] for m in inbox}  # nothing from the declined task

        # Restart recovery: the next call restarts the stopped service; state survives.
        assert service.stop(profile_dir("default", ctx)) == "stopped"
        result = body(await claude_chat.call_tool("connector_delegate", {**task, "target_chat": "Reviewer",
                                                                         "binding_revision": 1}))
        assert result["channel"] == channel  # same request key resumes the same task

        # Re-binding to chat B affects only new authorizations.
        assert body(await codex_app.call_tool("connector_bind_this_chat", {"label": "Reviewer B", "request_key": "b3"},
                                              meta=CHAT_B))["revision"] == 2
        stale = await claude_chat.call_tool("connector_delegate", {**task, "request_key": "t3",
                                                                   "target_chat": "Reviewer", "binding_revision": 1})
        assert stale.is_error and "target_binding_changed" in stale.content[0].text
        fresh = body(await claude_chat.call_tool("connector_delegate", {**task, "request_key": "t4",
                                                                        "target_chat": "Reviewer B", "binding_revision": 2}))
        assert [m["channel"] for m in body(await codex_app.call_tool(
            "connector_receive", {"timeout": 10}, meta=CHAT_B))["messages"]] == [fresh["channel"]]

    # Setting up again changes nothing; uninstall removes only what this installation owns.
    assert run_cli(environ, "setup", "codex")["changed"] is False
    assert run_cli(environ, "doctor", "--host", "codex")["ready"] is True
    removed = run_cli(environ, "uninstall", "--purge")
    assert {r["state"] for r in removed["host_entries"]} == {"removed"} and removed["data_dir"] == "removed"
    assert "local_ai_connector" not in tomlkit.parse(HOSTS["codex"].config_path(env).read_text()).get("mcp_servers", {})
