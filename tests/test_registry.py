import json
import sys

import pytest

from local_ai_connector.cli import main
from local_ai_connector.core import Broker, ConnectorError
from local_ai_connector.identity import caller_session
from local_ai_connector import os_adapter
from local_ai_connector.registry import add_peer
from local_ai_connector.server import create_app


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(tmp_path), "init", "--peers", "writer", "reviewer"])
    main()
    return tmp_path


async def test_register_arbitrary_worker_discover_and_exchange_after_restart(registry):
    profile = {"name": "Proof reader", "description": "Checks factual claims", "capabilities": ["fact-check"]}
    endpoint = add_peer(registry, "custom-worker", profile)
    config = json.loads(endpoint.read_text())
    assert config["client"] == "generic"
    assert os_adapter.is_private(endpoint)
    server = json.loads((registry / "server.json").read_text())
    broker = Broker(registry / "state.sqlite3")
    for peer, token in server["peers"].items():
        broker.register(peer, token, server.get("peer_profiles", {}).get(peer))
    try:
        status = broker.snapshot("writer")
        worker = next(w for w in status["workers"] if w["id"] == "custom-worker")
        assert worker == {"id": "custom-worker", **profile, "availability": "unknown", "conversation_modes": ["existing"]}
        assert status["self"] == "writer"
        assert config["token"] not in json.dumps(status)
        channel = await broker.open("writer", worker["id"], "Check this claim", "task")
        assert not (await broker.receive(worker["id"], timeout=0))["messages"]
        await broker.decide(channel["id"], True)
        question = (await broker.receive(worker["id"], timeout=0))["messages"][0]
        await broker.send(worker["id"], channel["id"], "answer", "Checked", "reply", question["id"])
        answer = (await broker.receive("writer", channel["id"], timeout=0))["messages"][0]
        assert answer["body"] == "Checked" and answer["reply_to"] == question["id"]
    finally:
        broker.close()
    # The real application must load the same owner profile at startup.
    app = create_app(registry)
    assert app.state.broker.snapshot()["workers"][0]["id"] == "custom-worker"
    app.state.broker.close()


def test_running_service_and_duplicate_registration_preserve_credentials(registry):
    before = (registry / "server.json").read_bytes()
    with os_adapter.exclusive_lock(registry / "server.lock"):
        with pytest.raises(ValueError, match="Stop"):
            add_peer(registry, "new-worker", {})
    assert not (registry / "new-worker.json").exists()
    with pytest.raises(ValueError, match="already exists"):
        add_peer(registry, "writer", {})
    assert (registry / "server.json").read_bytes() == before


@pytest.mark.parametrize("profile", [{"capabilities": [42]}, {"capabilities": [""]}, {"token": "secret"}, {"description": 3}])
def test_invalid_profiles_do_not_register_partial_endpoints(registry, profile):
    before = (registry / "server.json").read_bytes()
    with pytest.raises(ValueError):
        add_peer(registry, "new-worker", profile)
    assert not (registry / "new-worker.json").exists()
    assert (registry / "server.json").read_bytes() == before


def test_unknown_host_uses_owner_defined_session_mapping(registry):
    identity = {"namespace": "custom-host", "path": ["example/context", "conversation", "id"]}
    config = json.loads(add_peer(registry, "custom-native", {}, identity).read_text())
    assert config["client"] == "metadata"
    metadata = {"example/context": {"conversation": {"id": "session-a"}}}
    assert caller_session(config["client"], metadata, config["identity"]) == "custom-host:session-a"
    with pytest.raises(ConnectorError, match="身份"):
        caller_session("metadata", {}, identity)


def test_failed_config_replace_rolls_back_new_credentials(registry, monkeypatch):
    before = (registry / "server.json").read_bytes()
    def failed_replace(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr("local_ai_connector.registry.os.replace", failed_replace)
    with pytest.raises(OSError, match="disk failure"):
        add_peer(registry, "new-worker", {})
    assert not (registry / "new-worker.json").exists()
    assert (registry / "server.json").read_bytes() == before
    assert not list(registry.glob("registry-*.tmp"))


def test_cli_registers_profile_and_custom_identity(registry, monkeypatch, capsys):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(registry), "peer-add", "third-worker",
                                    "--name", "Reviewer", "--description", "Reviews code", "--capability", "review",
                                    "--session-namespace", "new-host", "--session-path", "context", "session"])
    main()
    config = json.loads((registry / "third-worker.json").read_text())
    output = capsys.readouterr().out
    assert "third-worker" in output and config["token"] not in output
    assert config["identity"] == {"namespace": "new-host", "path": ["context", "session"]}
    assert json.loads((registry / "server.json").read_text())["peer_profiles"]["third-worker"]["capabilities"] == ["review"]


@pytest.mark.parametrize("identity", [{}, {"namespace": "x", "path": []}, {"namespace": "x:y", "path": ["id"]}, {"namespace": "x", "path": "id"}])
def test_invalid_identity_does_not_silently_become_generic(registry, identity):
    with pytest.raises(ValueError):
        add_peer(registry, "custom-native", {}, identity)
    assert not (registry / "custom-native.json").exists()


@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
def test_cli_registers_locale_only_in_new_private_endpoint(registry, monkeypatch, locale):
    existing = (registry / "writer.json").read_bytes()
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(registry), "peer-add", "localized-worker",
                                    "--name", "Reviewer", "--mcp-locale", locale])
    main()
    endpoint = json.loads((registry / "localized-worker.json").read_text())
    assert endpoint["mcp_locale"] == locale
    assert endpoint["client"] == "generic"
    assert (registry / "writer.json").read_bytes() == existing
    profile = json.loads((registry / "server.json").read_text())["peer_profiles"]["localized-worker"]
    assert profile == {"name": "Reviewer", "description": "", "capabilities": []}


@pytest.mark.parametrize("locale", [None, "fr-FR", ["en-US"]])
def test_invalid_locale_does_not_write_registration(registry, locale):
    before = (registry / "server.json").read_bytes()
    with pytest.raises(ValueError, match="mcp_locale"):
        add_peer(registry, "localized-worker", {}, mcp_locale=locale)
    assert not (registry / "localized-worker.json").exists()
    assert (registry / "server.json").read_bytes() == before


def test_locale_registration_still_requires_stopped_service(registry):
    before = (registry / "server.json").read_bytes()
    with os_adapter.exclusive_lock(registry / "server.lock"):
        with pytest.raises(ValueError, match="Stop"):
            add_peer(registry, "localized-worker", {}, mcp_locale="en-US")
    assert not (registry / "localized-worker.json").exists()
    assert (registry / "server.json").read_bytes() == before
