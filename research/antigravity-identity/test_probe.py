"""Offline checks of the identity diagnostic. They simulate host payloads; they prove nothing about Antigravity."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyze  # noqa: E402
import make_workspace  # noqa: E402


def run_hook(event, payload, log, env=None):
    result = subprocess.run([sys.executable, str(HERE / "hook.py"), event, str(log)], input=json.dumps(payload),
                            capture_output=True, text=True, check=True, env=env)
    return json.loads(result.stdout)


@pytest.mark.parametrize("args, shape", [
    ({"note": "A1", "attestation": "FORGED"}, "top-level"),
    ({"Arguments": {"note": "A1"}, "ServerName": "identity_probe"}, "nested:Arguments"),
    ({"argumentsJson": json.dumps({"note": "A1"}), "serverName": "identity_probe"}, "json-string:argumentsJson"),
])
def test_hook_overwrites_attestation_in_each_argument_shape(tmp_path, args, shape):
    log = tmp_path / "hooks.jsonl"
    out = run_hook("pre", {"conversationId": "conv-a", "toolCall": {"name": "mcp_identity_probe", "args": args}}, log)
    entry = json.loads(log.read_text())
    assert out["decision"] == "ask" and entry["overwrite_shape"] == shape
    injected = json.dumps(out["overwrite"])
    assert entry["nonce"] in injected and "FORGED" not in injected


def test_hook_leaves_other_tools_to_normal_permissions(tmp_path):
    out = run_hook("pre", {"conversationId": "c", "toolCall": {"name": "mcp_other", "args": {"x": 1}}}, tmp_path / "h")
    assert out == {"decision": "ask"}


def test_hook_redacts_secret_looking_host_variables(tmp_path):
    log = tmp_path / "hooks.jsonl"
    env = {**__import__("os").environ, "ANTIGRAVITY_CSRF_TOKEN": "s3cret", "ANTIGRAVITY_CONVERSATION_ID": "conv-a"}
    run_hook("post", {"conversationId": "conv-a"}, log, env=env)
    entry = json.loads(log.read_text())
    assert entry["env"]["ANTIGRAVITY_CSRF_TOKEN"] == "<present>" and "s3cret" not in log.read_text()
    assert entry["env"]["ANTIGRAVITY_CONVERSATION_ID"] == "conv-a"


async def test_probe_server_records_arguments_and_analysis_correlates_by_nonce(tmp_path):
    agents, logs = make_workspace.build(tmp_path / "ws")
    config = json.loads((agents / "mcp_config.json").read_text())["mcpServers"]["identity_probe"]
    hooks = json.loads((agents / "hooks.json").read_text())["connector-identity-probe"]
    assert hooks["PreToolUse"][0]["matcher"] == "call_mcp_tool|mcp_.*"
    out = run_hook("pre", {"conversationId": "conv-a", "toolCall": {"name": "mcp_identity_probe", "args": {"note": "A1"}}},
                   logs / "hooks.jsonl")
    async with Client(StdioServerParameters(command=config["command"], args=config["args"])) as client:
        result = await client.call_tool("identity_probe", {"note": "A1", **out["overwrite"]}, meta={"k": "v"})
        assert json.loads(result.content[0].text)["attestation"] == out["overwrite"]["attestation"]
    report = analyze.analyze(logs)
    assert report["calls"][0]["hook_conversation"] == "conv-a" and "k" in report["calls"][0]["meta_keys"]


def test_hook_denies_the_protected_server_only(tmp_path):
    import subprocess as sp
    def pre(server):
        payload = {"conversationId": "c", "toolCall": {"name": "call_mcp_tool", "args": {"ServerName": server, "Arguments": {}}}}
        result = sp.run([sys.executable, str(HERE / "hook.py"), "pre", str(tmp_path / "h"), "--deny-server", "live"],
                        input=json.dumps(payload), capture_output=True, text=True, check=True)
        return json.loads(result.stdout)["decision"]
    assert pre("live") == "deny" and pre("live_test") == "ask"
