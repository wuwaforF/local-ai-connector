import json
import os

import pytest

from test_claude_guided_binding import isolated
from test_claude_onboarding import open_inbox, world
from integrations.claude_code import desktop_binding, guided_binding, onboarding
from local_ai_connector import claude_binding as bridge


def test_catalog_is_read_only_and_exposes_only_desktop_choice_fields(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    before = {path.name: path.read_bytes() for path in setup.data.iterdir() if path.is_file()}

    result = desktop_binding.catalog(setup.data, setup.home)

    assert result["status"] == "catalog_ready"
    assert len(result["choices"]) == 1
    choice = result["choices"][0]
    assert set(choice) == {"desktop_id", "title", "workspace", "selection_revision"}
    assert choice["desktop_id"] == setup.desktop_id
    serialized = json.dumps(result)
    for secret in (setup.cli_id, "test-peer-token", "test-admin-token", str(world.inbox_path)):
        assert secret not in serialized
    assert {path.name: path.read_bytes() for path in setup.data.iterdir() if path.is_file()} == before


def test_new_target_fails_closed_without_capture_commit_or_socket_inspection(
        world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    before = {path.name: path.read_bytes() for path in setup.data.iterdir() if path.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("new desktop targets must not invoke native enrollment")

    monkeypatch.setattr(onboarding, "capture", forbidden)
    monkeypatch.setattr(guided_binding, "commit", forbidden)
    monkeypatch.setattr(bridge, "socket_identity", forbidden)
    result = desktop_binding.inspect(setup.root, setup.data, setup.home, setup.desktop_id,
        desktop_binding.catalog(setup.data, setup.home)["choices"][0]["selection_revision"])

    assert result["status"] == "native_authorization_unavailable"
    assert setup.cli_id not in json.dumps(result)
    assert {path.name: path.read_bytes() for path in setup.data.iterdir() if path.is_file()} == before
    assert not (setup.data / f"{setup.cli_id}.json").exists()


def _bind_selected_to_existing_record(setup):
    record = setup.data / f"{setup.cli_id}.json"
    info = os.stat(setup.receipt["messaging_socket"], follow_symlinks=False)
    record.write_text(json.dumps({
        "session_id": setup.cli_id,
        "workspace": str(setup.workspace),
        "socket": setup.receipt["messaging_socket"],
        "socket_dev": info.st_dev,
        "socket_ino": info.st_ino,
        "desktop_id": setup.desktop_id,
        "source": "explicit socket observed inside selected Claude chat",
    }))
    record.chmod(0o600)
    wake_path = setup.data / "wakeup.json"
    wake = json.loads(wake_path.read_text())
    wake["bindings"]["claude_code"]["target"] = {
        "session_id": setup.cli_id, "workspace": str(setup.workspace)}
    wake["bindings"]["claude_code"]["command"][-1] = str(record)
    wake_path.write_text(json.dumps(wake))
    return record


def test_same_existing_target_verifies_through_bridge_contract(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    _bind_selected_to_existing_record(setup)

    result = desktop_binding.inspect(setup.root, setup.data, setup.home, setup.desktop_id,
        desktop_binding.catalog(setup.data, setup.home)["choices"][0]["selection_revision"])

    assert result["status"] == "existing_binding_verified"
    assert result["changes"] == []
    assert setup.cli_id not in json.dumps(result)


def test_same_target_with_replaced_socket_is_stale_and_does_not_rewrite_config(
        world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    _bind_selected_to_existing_record(setup)
    before = {name: (setup.data / name).read_bytes()
              for name in ("server.json", "wakeup.json", "claude_code.json")}
    world.inbox_listener.close()
    world.inbox_path.unlink()
    replacement_listener, _ = open_inbox(world.record_dir, world.inbox_path.name)
    try:
        result = desktop_binding.inspect(setup.root, setup.data, setup.home, setup.desktop_id,
        desktop_binding.catalog(setup.data, setup.home)["choices"][0]["selection_revision"])
        assert result["status"] == "stale_binding"
        assert {name: (setup.data / name).read_bytes() for name in before} == before
    finally:
        replacement_listener.close()


def test_inspect_rejects_changed_or_missing_selected_metadata(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    revision = desktop_binding.catalog(setup.data, setup.home)["choices"][0]["selection_revision"]
    setup.metadata.write_text(json.dumps({"sessionId": setup.desktop_id, "isArchived": True}))
    assert desktop_binding.inspect(setup.root, setup.data, setup.home, setup.desktop_id, revision)["status"] == "catalog_stale"
    assert desktop_binding.inspect(setup.root, setup.data, setup.home, "local_missing", revision)["status"] == "catalog_stale"


def test_catalog_reports_mismatched_endpoint_without_disclosing_token(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    endpoint_path = setup.data / "claude_code.json"
    endpoint = json.loads(endpoint_path.read_text())
    endpoint["token"] = "deliberately-not-matching-secret"
    endpoint_path.write_text(json.dumps(endpoint))

    with pytest.raises(ValueError, match="配置不匹配") as error:
        desktop_binding.catalog(setup.data, setup.home)
    assert "deliberately-not-matching-secret" not in str(error.value)


