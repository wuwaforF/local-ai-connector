import json
import sqlite3
from pathlib import Path

from local_ai_connector.adapter_socket import SocketAdapter
from local_ai_connector.claude_binding import compatibility_issues
from local_ai_connector.conversations import Conversations
from local_ai_connector.wakeup import CommandAdapter, parse_config
import pytest

pytestmark = pytest.mark.posix  # Unix sockets, owner/mode bits or fcntl

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/configs"
PEERS = {"gpt", "codex_desktop", "gemini", "claude_code"}


def load(name):
    return json.loads((EXAMPLES / name).read_text())


def test_wakeup_example_loads_with_real_parser():
    bindings, options = parse_config(load("wakeup.example.json"), PEERS)

    assert options == {"send_when_unknown": True}
    assert isinstance(bindings["codex_desktop"].adapter, CommandAdapter)
    assert isinstance(bindings["gemini"].adapter, SocketAdapter)
    assert bindings["claude_code"].adapter.command[2] == "--session-record"
    assert compatibility_issues(bindings, options) == []


def test_conversation_registrations_example_is_accepted():
    registrations = load("server-conversations.example.json")["conversations"]
    db = sqlite3.connect(":memory:")
    try:
        assert Conversations(db, registrations).registrations == registrations
    finally:
        db.close()


def test_launcher_examples_contain_placeholders_only():
    for name in ("mcp-stdio-source-checkout.example.json", "antigravity-sidecar.example.json"):
        text = (EXAMPLES / name).read_text()
        assert "/absolute/path/to/" in text
        assert "/Users/" not in text and "token" not in text.lower()
    entry = load("mcp-stdio-source-checkout.example.json")["mcpServers"]["local_ai_connector"]
    assert entry["args"][:4] == ["-m", "local_ai_connector.cli", "mcp", "--config"]
