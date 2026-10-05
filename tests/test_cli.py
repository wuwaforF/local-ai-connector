import json
import stat
import sys
import tomllib

import pytest

from local_ai_connector.cli import main


def test_multi_peer_init_and_client_configs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(tmp_path), "init", "--peers", "writer", "reviewer", "tester", "native", "--native-peer", "native=codex"])
    main()
    capsys.readouterr()
    server = json.loads((tmp_path / "server.json").read_text())
    assert set(server["peers"]) == {"writer", "reviewer", "tester", "native"}
    assert len(set(server["peers"].values())) == 4
    for peer, token in server["peers"].items():
        path = tmp_path / f"{peer}.json"
        config = json.loads(path.read_text())
        assert config["peer"] == peer and config["token"] == token
        assert config["client"] == ("codex" if peer == "native" else "generic")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    original = (tmp_path / "writer.json").read_bytes()
    for client in ("generic", "codex", "zcode"):
        monkeypatch.setattr(sys, "argv", ["connector", "--data", str(tmp_path), "client-config", "--peer", "writer", "--client", client])
        main()
        output = capsys.readouterr().out
        if client == "codex":
            entry = tomllib.loads(output)["mcp_servers"]["local_ai_connector"]
        elif client == "zcode":
            entry = json.loads(output)["mcp"]["servers"]["local_ai_connector"]
        else:
            entry = json.loads(output)["mcpServers"]["local_ai_connector"]
        assert entry["command"] == sys.executable
        assert entry["args"] == ["-m", "local_ai_connector.cli", "mcp", "--config", str(tmp_path / "writer.json")]
    for client in ("generic", "codex"):
        monkeypatch.setattr(sys, "argv", ["connector", "--data", str(tmp_path), "client-config", "--peer", "writer", "--client", client, "--transport", "streamable-http"])
        main()
        output = capsys.readouterr().out
        entry = tomllib.loads(output)["mcp_servers"]["local_ai_connector"] if client == "codex" else json.loads(output)["mcpServers"]["local_ai_connector"]
        assert entry["url"] == server["url"] + "/mcp"
        headers = entry["http_headers"] if client == "codex" else entry["headers"]
        assert headers == {"Authorization": "Bearer " + server["peers"]["writer"]}
    assert (tmp_path / "writer.json").read_bytes() == original
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(tmp_path), "client-config", "--peer", "native", "--transport", "streamable-http"])
    with pytest.raises(SystemExit):
        main()
    assert "仅使用 stdio" in capsys.readouterr().err


@pytest.mark.parametrize("args", [
    ["--peers", "only"],
    ["--peers", "a", "a"],
    ["--peers", "a", "../escape"],
    ["--peers", "a", "server"],
    ["--peers", "model", "b"],
    ["--peers", "a", "b", "--native-peer", "missing=codex"],
    ["--peers", "a", "b", "--native-peer", "a=unknown"],
    ["--peers", "a", "b", "--native-peer", "a=codex", "--native-peer", "a=zcode"],
])
def test_invalid_init_does_not_write_partial_credentials(tmp_path, monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(tmp_path), "init", *args])
    with pytest.raises(SystemExit):
        main()
    assert list(tmp_path.iterdir()) == []


def test_init_preserves_existing_endpoint(tmp_path, monkeypatch):
    path = tmp_path / "writer.json"
    path.write_text('existing credentials')
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(tmp_path), "init", "--peers", "writer", "reviewer"])
    with pytest.raises(SystemExit):
        main()
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_text() == 'existing credentials'
