import json
import os
from pathlib import Path
import sys

import pytest

from hostfakes import grant_others
from local_ai_connector import os_adapter
from local_ai_connector.cli import main
from local_ai_connector.registry import enable_chat_approval


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(tmp_path), "init", "--peers", "first", "second", "gpt"])
    main()
    server_path = tmp_path / "server.json"
    server = json.loads(server_path.read_text())
    server["approval_tokens"] = {"gpt": "existing-gpt-approval"}
    server["peer_profiles"] = {"first": {"name": "Existing name"}}
    server_path.write_text(json.dumps(server))
    gpt = json.loads((tmp_path / "gpt.json").read_text())
    gpt.update(tool_profile="requester", approval_token="existing-gpt-approval")
    (tmp_path / "gpt.json").write_text(json.dumps(gpt))
    (tmp_path / "wakeup.json").write_text('{"bindings":{"first":{"target":"retained"}}}')
    return tmp_path


def snapshot(data):
    return {path.name: path.read_bytes() for path in data.glob("*.json")}


def test_migrate_two_arbitrary_peers_preserves_credentials_and_other_state(configured):
    before = snapshot(configured)
    paths = enable_chat_approval(configured, ["first", "second"])
    after = snapshot(configured)
    assert after["gpt.json"] == before["gpt.json"]
    assert after["wakeup.json"] == before["wakeup.json"]
    old_server, server = json.loads(before["server.json"]), json.loads(after["server.json"])
    assert {k: v for k, v in server.items() if k != "approval_tokens"} == {k: v for k, v in old_server.items() if k != "approval_tokens"}
    assert server["approval_tokens"]["gpt"] == "existing-gpt-approval"
    assert len(set(server["approval_tokens"].values())) == 3
    for peer, path in zip(("first", "second"), paths):
        old, new = json.loads(before[path.name]), json.loads(after[path.name])
        assert new == {**old, "tool_profile": "participant", "mcp_locale": "en-US", "approval_token": server["approval_tokens"][peer]}
        assert new["approval_token"] not in {server["admin_token"], *server["peers"].values()}
        assert os_adapter.is_private(path)
    assert os_adapter.is_private(configured / "server.json")
    enable_chat_approval(configured, ["first", "second"])
    assert snapshot(configured) == after
    assert not list(configured.glob("approval-*.tmp"))


def test_identity_mapping_and_bound_session_are_preserved(configured):
    path = configured / "first.json"
    endpoint = json.loads(path.read_text())
    endpoint.update(client="metadata", identity={"namespace": "custom", "path": ["context", "session"]}, bound_session="custom:abc")
    path.write_text(json.dumps(endpoint))
    enable_chat_approval(configured, ["first"], mcp_locale="zh-CN")
    updated = json.loads(path.read_text())
    assert all(updated[key] == value for key, value in endpoint.items())
    assert updated["mcp_locale"] == "zh-CN"


def test_live_service_lock_refuses_without_writes(configured):
    before = snapshot(configured)
    with os_adapter.exclusive_lock(configured / "server.lock"):
        with pytest.raises(ValueError, match="Stop"):
            enable_chat_approval(configured, ["first"])
    assert snapshot(configured) == before


@pytest.mark.parametrize("mutation", ["peer", "token", "url", "client", "metadata", "requester", "unknown-profile", "peer-token", "participant-missing", "participant-mismatch", "duplicate-scoped", "ordinary-overlap", "unknown-scoped-peer"])
def test_invalid_configuration_preserves_all_files(configured, mutation):
    path = configured / "second.json"
    endpoint = json.loads(path.read_text())
    server_path = configured / "server.json"
    server = json.loads(server_path.read_text())
    if mutation in ("peer", "token", "url", "client"):
        endpoint[mutation] = "incorrect"
    elif mutation == "metadata":
        endpoint.update(client="metadata", identity={})
    elif mutation in ("requester", "unknown-profile"):
        endpoint["tool_profile"] = mutation
    elif mutation == "peer-token":
        endpoint["approval_token"] = "stray-token"
    elif mutation.startswith("participant"):
        endpoint.update(tool_profile="participant", approval_token="different")
        if mutation.endswith("mismatch"):
            server["approval_tokens"]["second"] = "server-value"
    else:
        key = "unregistered" if mutation == "unknown-scoped-peer" else "second"
        server["approval_tokens"][key] = server["peers"]["first"] if mutation == "ordinary-overlap" else "existing-gpt-approval"
    path.write_text(json.dumps(endpoint))
    server_path.write_text(json.dumps(server))
    before = snapshot(configured)
    with pytest.raises(ValueError):
        enable_chat_approval(configured, ["first", "second"])
    assert snapshot(configured) == before
    assert not list(configured.glob("approval-*.tmp"))


@pytest.mark.parametrize("peers,locale", [([], "en-US"), (["first", "first"], "en-US"), (["first"], "fr-FR"), (["missing"], "en-US")])
def test_invalid_selection_does_not_change_configs(configured, peers, locale):
    before = snapshot(configured)
    with pytest.raises(ValueError):
        enable_chat_approval(configured, peers, mcp_locale=locale)
    assert snapshot(configured) == before


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_atomic_replace_failures_restore_exact_originals(configured, monkeypatch, fail_at):
    before = snapshot(configured)
    original_replace = os.replace
    calls = 0
    def replace(source, target):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("synthetic replacement failure")
        return original_replace(source, target)
    monkeypatch.setattr("local_ai_connector.registry.os.replace", replace)
    with pytest.raises(OSError, match="synthetic"):
        enable_chat_approval(configured, ["first", "second"])
    assert snapshot(configured) == before
    assert not list(configured.glob("approval-*.tmp"))


def test_stage_failure_occurs_before_any_commit(configured, monkeypatch):
    before = snapshot(configured)
    monkeypatch.setattr("local_ai_connector.registry.os.fsync", lambda _: (_ for _ in ()).throw(OSError("synthetic fsync failure")))
    with pytest.raises(OSError, match="fsync"):
        enable_chat_approval(configured, ["first", "second"])
    assert snapshot(configured) == before
    assert not list(configured.glob("approval-*.tmp"))


def test_failed_rollback_retains_private_recovery_copy(configured, monkeypatch):
    before = snapshot(configured)
    original_replace = os.replace
    calls = 0
    def replace(source, target):
        nonlocal calls
        calls += 1
        if calls in (2, 3):
            raise OSError("synthetic storage failure")
        return original_replace(source, target)
    monkeypatch.setattr("local_ai_connector.registry.os.replace", replace)
    with pytest.raises(BaseExceptionGroup, match="recovery copies") as error:
        enable_chat_approval(configured, ["first", "second"])
    report = " ".join(str(exc) for exc in error.value.exceptions)
    copies = list(configured.glob("approval-*.tmp"))
    server_copy = next(path for path in copies if path.read_bytes() == before["server.json"])
    assert str(server_copy) in report
    assert all(os_adapter.is_private(path) for path in copies)
    assert all(token not in report for token in json.loads(before["server.json"])["peers"].values())
    assert (configured / "first.json").read_bytes() == before["first.json"]


def test_existing_participant_repairs_only_private_file_mode(configured):
    enable_chat_approval(configured, ["first"])
    before = snapshot(configured)
    grant_others(configured / "first.json", write=False)
    enable_chat_approval(configured, ["first"])
    assert snapshot(configured) == before
    assert os_adapter.is_private(configured / "first.json")


@pytest.mark.skipif(sys.platform == "win32", reason="creating symbolic links needs privileges on Windows")
def test_symlink_configuration_is_not_replaced(configured):
    source = configured / "first.json"
    target = configured / "real-first.json"
    source.rename(target)
    source.symlink_to(target)
    before = snapshot(configured)
    with pytest.raises(ValueError, match="symbolic"):
        enable_chat_approval(configured, ["first"])
    assert source.is_symlink() and snapshot(configured) == before


def test_cli_does_not_print_credentials(configured, monkeypatch, capsys):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["connector", "--data", str(configured), "enable-chat-approval", "first", "second"])
    main()
    output = capsys.readouterr().out
    assert "first" in output and "second" in output
    server = json.loads((configured / "server.json").read_text())
    assert not any(token in output for token in server["approval_tokens"].values())
