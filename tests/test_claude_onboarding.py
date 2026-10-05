import asyncio
import json
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.claude_code import onboarding
from integrations.codex_desktop import conversation_deployment
from integrations.claude_code.bridge import confirmed_session
from local_ai_connector.core import Broker


def open_inbox(directory, name):
    path = directory / name
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)
    return listener, path


@pytest.fixture
def world(tmp_path):
    with tempfile.TemporaryDirectory(prefix="claude-onboard-", dir="/tmp") as temp:
        base = Path(temp)
        workspace = base / "workspace"
        workspace.mkdir()
        record_dir = workspace / ".connector"
        record_dir.mkdir(mode=0o700)
        desktop_id, cli_id = str(uuid4()), str(uuid4())
        metadata = base / f"{desktop_id}.json"
        metadata.write_text(json.dumps({
            "sessionId": desktop_id,
            "cliSessionId": cli_id,
            "cwd": str(workspace),
            "originCwd": str(workspace),
            "isArchived": False,
        }))
        metadata.chmod(0o600)
        listener, inbox_path = open_inbox(record_dir, "inbox.sock")
        record = record_dir / f"{cli_id}.json"
        info = inbox_path.stat()
        record.write_text(json.dumps({
            "session_id": cli_id,
            "workspace": str(workspace),
            "socket": str(inbox_path),
            "socket_dev": info.st_dev,
            "socket_ino": info.st_ino,
            "desktop_id": desktop_id,
            "source": "explicit socket observed inside selected Claude chat",
        }))
        record.chmod(0o600)

        data = tmp_path / "connector-data"
        data.mkdir()
        root = ROOT
        old_id = str(uuid4())
        old_workspace = str(base / "previous-workspace")
        old_record = record_dir / f"{old_id}.json"
        bridge = root / "integrations/claude_code/bridge.py"
        old_binding = {
            "adapter": "command",
            "target": {"session_id": old_id, "workspace": old_workspace},
            "timeout": 10,
            "command": [str(root / ".venv/bin/python"), str(bridge), "--session-record", str(old_record)],
        }
        server = {
            "url": "http://127.0.0.1:23456",
            "port": 23456,
            "peers": {"claude_code": "test-peer-token", "gemini": "test-gemini-token"},
            "admin_token": "test-admin-token",
        }
        endpoint = {
            "url": server["url"],
            "token": server["peers"]["claude_code"],
            "peer": "claude_code",
            "client": "generic",
            "mcp_locale": "en-US",
        }
        wake = {"enabled": True, "send_when_unknown": True, "bindings": {
            "claude_code": old_binding,
            "gemini": {"adapter": "command", "target": {"session_id": "gemini-session"},
                       "command": ["/tmp/gemini-bridge"], "timeout": 10},
        }}
        paths = {
            data / "server.json": server,
            data / "wakeup.json": wake,
            data / "claude_code.json": endpoint,
        }
        for path, value in paths.items():
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        broker = Broker(data / "state.sqlite3")
        broker.close()

        fixture = SimpleNamespace(root=root, data=data, server=server, wake=wake, endpoint=endpoint,
            metadata=metadata, desktop_id=desktop_id, cli_id=cli_id, workspace=workspace,
            record=record, record_dir=record_dir, inbox_path=inbox_path, inbox_listener=listener,
            old_binding=old_binding, old_id=old_id, old_workspace=old_workspace, base=base)
        try:
            yield fixture
        finally:
            listener.close()


def write_metadata(world, changes):
    metadata = json.loads(world.metadata.read_text())
    for key, value in changes.items():
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    world.metadata.write_text(json.dumps(metadata))


def read_json(path):
    return json.loads(path.read_text())


def test_capture_preview_is_read_only_and_apply_writes_private_live_record(world):
    world.record.unlink()
    preview = onboarding.capture(world.metadata, world.desktop_id, world.cli_id, world.workspace,
        world.record, world.inbox_path)
    assert preview["mode"] == "capture_preview"
    assert not world.record.exists()

    applied = onboarding.capture(world.metadata, world.desktop_id, world.cli_id, world.workspace,
        world.record, world.inbox_path, write=True)
    assert applied["mode"] == "captured"
    assert world.record.stat().st_mode & 0o777 == 0o600
    assert confirmed_session({"session_id": world.cli_id, "workspace": str(world.workspace)}, world.record)[
        "session_id"] == world.cli_id


@pytest.mark.parametrize("changes", [
    {"session_id": str(uuid4())},
    {"workspace": "/another/workspace"},
])
def test_capture_refuses_recapturing_a_different_session_or_workspace(world, changes):
    previous = read_json(world.record)
    previous.update(changes)
    world.record.write_text(json.dumps(previous))
    world.record.chmod(0o600)
    original = world.record.read_bytes()

    with pytest.raises(ValueError, match="different session/workspace"):
        onboarding.capture(world.metadata, world.desktop_id, world.cli_id, world.workspace,
            world.record, world.inbox_path, write=True)
    assert world.record.read_bytes() == original


def test_capture_allows_same_session_recapture_with_current_inbox(world):
    replacement_listener, replacement_path = open_inbox(world.record_dir, "resumed.sock")
    try:
        result = onboarding.capture(world.metadata, world.desktop_id, world.cli_id, world.workspace,
            world.record, replacement_path, write=True)
        assert result["mode"] == "captured"
        current = read_json(world.record)
        assert current["socket"] == str(replacement_path)
        assert confirmed_session({"session_id": world.cli_id, "workspace": str(world.workspace)}, world.record)[
            "socket_path"] == str(replacement_path)
    finally:
        replacement_listener.close()


def test_prepare_accepts_arbitrary_distinct_desktop_and_cli_ids_and_preserves_other_config(world):
    assert world.desktop_id != world.cli_id
    server_path, wake_path, endpoint_path = (world.data / name for name in
        ("server.json", "wakeup.json", "claude_code.json"))
    old_digest = onboarding.binding_digest(world.old_binding)
    originals, replacements, report = onboarding.prepare(world.root, world.data, world.metadata,
        world.desktop_id, world.cli_id, world.workspace, world.record, replace_binding=old_digest)

    assert report["target"] == {"session_id": world.cli_id, "workspace": str(world.workspace)}
    assert report["binding_changes"] is True
    assert set(replacements) == {wake_path}
    assert originals[server_path] == server_path.read_bytes()
    assert originals[endpoint_path] == endpoint_path.read_bytes()
    proposed_wake = json.loads(replacements[wake_path])
    assert proposed_wake["bindings"]["gemini"] == world.wake["bindings"]["gemini"]
    assert proposed_wake["bindings"]["claude_code"]["target"] == report["target"]
    assert read_json(wake_path) == world.wake


def test_replacement_preview_returns_digest_and_wrong_digest_is_refused(world):
    args = (world.root, world.data, world.metadata, world.desktop_id, world.cli_id,
            world.workspace, world.record)
    originals, replacements, report = onboarding.prepare(*args)
    assert replacements
    assert report["replacement_requires_confirmation"] is True
    assert report["previous_binding_sha256"] == onboarding.binding_digest(world.old_binding)
    with pytest.raises(ValueError, match="previous binding changed"):
        onboarding.prepare(*args, replace_binding="0" * 64)


@pytest.mark.parametrize("changes", [
    {"isArchived": True},
    {"cwd": "/another/workspace"},
    {"originCwd": None},
    {"cliSessionId": str(uuid4())},
    {"sessionId": str(uuid4())},
])
def test_target_rejects_archived_or_mismatched_metadata(world, changes):
    write_metadata(world, changes)
    with pytest.raises(ValueError):
        onboarding.inspect_target(world.metadata, world.desktop_id, world.cli_id,
            world.workspace, world.record)


def test_matching_binding_still_verifies_the_live_socket(world):
    wake_path = world.data / "wakeup.json"
    wake = read_json(wake_path)
    wake["bindings"]["claude_code"] = {
        **world.old_binding,
        "target": {"session_id": world.cli_id, "workspace": str(world.workspace)},
        "command": [str(world.root / ".venv/bin/python"),
                    str(world.root / "integrations/claude_code/bridge.py"),
                    "--session-record", str(world.record)],
    }
    wake_path.write_text(json.dumps(wake))
    world.inbox_path.unlink()
    with pytest.raises(ValueError):
        onboarding.prepare(world.root, world.data, world.metadata, world.desktop_id,
            world.cli_id, world.workspace, world.record)


@pytest.mark.parametrize("status", ["pending", "active"])
def test_quiet_refuses_pending_and_active_channels(world, status):
    broker = Broker(world.data / "state.sqlite3")
    try:
        broker.register("claude_code", "synthetic-claude-token")
        broker.register("gemini", "synthetic-gemini-token")
        task = asyncio.run(broker.open("gemini", "claude_code", "synthetic task", "quiet-test"))
        if status == "active":
            asyncio.run(broker.decide(task["id"], True))
    finally:
        broker.close()

    with pytest.raises(ValueError, match="pending/active"):
        onboarding.quiet(world.data)


def test_install_uses_shared_apply_with_precommit_and_readiness_revalidation(world, monkeypatch):
    digest = onboarding.binding_digest(world.old_binding)
    originals, replacements, _ = onboarding.prepare(world.root, world.data, world.metadata,
        world.desktop_id, world.cli_id, world.workspace, world.record, replace_binding=digest)
    plist_path = world.base / "service.plist"
    expected = [str(world.root / ".venv/bin/python"), "-m", "local_ai_connector.cli",
                "--data", str(world.data), "serve"]
    label = "dev.local-ai-connector.service"
    plist_path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": expected,
        "EnvironmentVariables": {"PYTHONPATH": str(world.root / "src")}}))

    service = {"loaded": True, "pid": 123}
    listener_pids = iter(("123", "456"))
    events = []

    def fake_listener(port, command):
        assert port == world.server["port"]
        assert command == expected
        return next(listener_pids)

    def fake_run(argv, **kwargs):
        assert argv[0:2] == ["/bin/launchctl", "bootout"] or argv[0:2] == ["/bin/launchctl", "bootstrap"]
        if argv[1] == "bootout":
            service["loaded"] = False
            events.append("bootout")
        else:
            service["loaded"] = True
            events.append("bootstrap")
        return SimpleNamespace(returncode=0)

    def fake_check_output(argv, **kwargs):
        assert argv == ["/bin/launchctl", "list"]
        return f"123\t0\t{label}\n"

    monkeypatch.setattr(conversation_deployment, "listener", fake_listener)
    monkeypatch.setattr(conversation_deployment.subprocess, "run", fake_run)
    monkeypatch.setattr(conversation_deployment.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(conversation_deployment.time, "sleep", lambda _: None)

    shared_apply = onboarding.apply
    callbacks = {"precommit": 0, "readiness": 0}

    def observed_shared_apply(*args, precommit=None, readiness=None, **kwargs):
        assert precommit is not None and readiness is not None

        def observed_precommit():
            callbacks["precommit"] += 1
            return precommit()

        def observed_readiness():
            callbacks["readiness"] += 1
            return readiness()

        return shared_apply(*args, precommit=observed_precommit,
            readiness=observed_readiness, **kwargs)

    monkeypatch.setattr(onboarding, "apply", observed_shared_apply)
    onboarding.install(world.root, world.data, originals, replacements, world.metadata,
        world.desktop_id, world.cli_id, world.workspace, world.record, plist_path,
        replace_binding=digest)

    assert events == ["bootout", "bootstrap"]
    assert callbacks == {"precommit": 1, "readiness": 1}
    assert service["loaded"] is True
    assert read_json(world.data / "wakeup.json")["bindings"]["claude_code"]["target"] == {
        "session_id": world.cli_id, "workspace": str(world.workspace)}


def test_install_without_exact_confirmation_refuses_before_service_action(world, monkeypatch):
    originals, replacements, _ = onboarding.prepare(world.root, world.data, world.metadata,
        world.desktop_id, world.cli_id, world.workspace, world.record)
    actions = []
    monkeypatch.setattr(onboarding, "apply", lambda *args, **kwargs: actions.append("apply"))
    with pytest.raises(ValueError, match="exact inspected previous binding digest"):
        onboarding.install(world.root, world.data, originals, replacements, world.metadata,
            world.desktop_id, world.cli_id, world.workspace, world.record, world.base / "unused.plist")
    assert actions == []


@pytest.mark.parametrize("matching", [True, False])
def test_prepare_accepts_only_participant_endpoint_with_matching_approval_token(world, matching):
    server_path = world.data / "server.json"
    endpoint_path = world.data / "claude_code.json"
    server = read_json(server_path)
    server["approval_tokens"] = {"claude_code": "synthetic-scoped-approval-token"}
    endpoint = read_json(endpoint_path)
    endpoint["tool_profile"] = "participant"
    endpoint["approval_token"] = (server["approval_tokens"]["claude_code"] if matching
                                   else "synthetic-mismatched-approval-token")
    server_path.write_text(json.dumps(server))
    endpoint_path.write_text(json.dumps(endpoint))
    before = {path: path.read_bytes() for path in (server_path, world.data / "wakeup.json", endpoint_path)}

    if matching:
        digest = onboarding.binding_digest(world.old_binding)
        _, replacements, report = onboarding.prepare(world.root, world.data, world.metadata,
            world.desktop_id, world.cli_id, world.workspace, world.record, replace_binding=digest)
        assert report["binding_changes"] is True
        assert world.data / "wakeup.json" in replacements
    else:
        with pytest.raises(ValueError, match="approval endpoint does not match"):
            onboarding.prepare(world.root, world.data, world.metadata, world.desktop_id,
                world.cli_id, world.workspace, world.record)

    assert {path: path.read_bytes() for path in before} == before


def test_registration_wrapper_requires_identity_arguments_before_data_access(world):
    server_path = world.data / "server.json"
    before = server_path.read_bytes()
    wrapper = world.root / "deployment/register-claude-code.command"
    result = subprocess.run(["/bin/zsh", str(wrapper), "--data", str(world.data)],
        capture_output=True, text=True, timeout=10)

    assert result.returncode != 0
    assert "required" in result.stderr
    assert server_path.read_bytes() == before


@pytest.mark.parametrize(("mutation", "expected_issue"), [
    ("missing_enabled", "Wakeups must be explicitly enabled"),
    ("false_enabled", "Wakeups must be explicitly enabled"),
    ("missing_send_when_unknown", "send_when_unknown must be true"),
    ("false_send_when_unknown", "send_when_unknown must be true"),
    ("restore", "does not support restore"),
])
def test_unsafe_wakeup_options_are_reported_in_preview_and_rejected_before_service_action(
        world, monkeypatch, mutation, expected_issue):
    wake_path = world.data / "wakeup.json"
    wake = read_json(wake_path)
    if mutation == "missing_enabled":
        wake.pop("enabled")
    elif mutation == "false_enabled":
        wake["enabled"] = False
    elif mutation == "missing_send_when_unknown":
        wake.pop("send_when_unknown")
    elif mutation == "false_send_when_unknown":
        wake["send_when_unknown"] = False
    else:
        wake["bindings"]["claude_code"]["restore"] = True
    wake_path.write_text(json.dumps(wake))
    original_bytes = {path: path.read_bytes() for path in (
        world.data / "server.json", wake_path, world.data / "claude_code.json")}
    original_binding = wake["bindings"]["claude_code"]
    digest = onboarding.binding_digest(original_binding)

    originals, replacements, report = onboarding.prepare(world.root, world.data, world.metadata,
        world.desktop_id, world.cli_id, world.workspace, world.record, replace_binding=digest)
    assert any(expected_issue in issue for issue in report["wake_compatibility_issues"])
    actions = []
    monkeypatch.setattr(onboarding, "apply", lambda *args, **kwargs: actions.append("apply"))
    monkeypatch.setattr(onboarding, "listener", lambda *args, **kwargs: actions.append("listener"))
    with pytest.raises(ValueError, match=expected_issue):
        onboarding.install(world.root, world.data, originals, replacements, world.metadata,
            world.desktop_id, world.cli_id, world.workspace, world.record,
            world.base / "unused.plist", replace_binding=digest)
    assert actions == []
    assert {path: path.read_bytes() for path in original_bytes} == original_bytes


@pytest.mark.parametrize("invalid", ["malformed", "unsupported"])
def test_prepare_rejects_invalid_candidate_binding(world, invalid):
    wake_path = world.data / "wakeup.json"
    wake = read_json(wake_path)
    binding = wake["bindings"]["claude_code"]
    if invalid == "malformed":
        binding["timeout"] = 0
    else:
        binding["future_option"] = "unsupported"
    wake_path.write_text(json.dumps(wake))

    with pytest.raises(ValueError):
        onboarding.prepare(world.root, world.data, world.metadata, world.desktop_id,
            world.cli_id, world.workspace, world.record)


@pytest.mark.parametrize("mismatch", ["target", "command"])
def test_readiness_mismatch_rolls_back_exact_config_and_restores_service(world, monkeypatch, mismatch):
    digest = onboarding.binding_digest(world.old_binding)
    originals, replacements, _ = onboarding.prepare(world.root, world.data, world.metadata,
        world.desktop_id, world.cli_id, world.workspace, world.record, replace_binding=digest)
    before = {path: path.read_bytes() for path in originals}
    plist_path = world.base / "service.plist"
    expected = [str(world.root / ".venv/bin/python"), "-m", "local_ai_connector.cli",
                "--data", str(world.data), "serve"]
    label = "dev.local-ai-connector.service"
    plist_path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": expected,
        "EnvironmentVariables": {"PYTHONPATH": str(world.root / "src")}}))

    service = {"loaded": True}
    pids = iter(("123", "456", "789"))
    events = []
    real_load_config = onboarding.load_config
    loads = []

    def fake_listener(port, command):
        assert (port, command) == (world.server["port"], expected)
        return next(pids)

    def fake_run(argv, **kwargs):
        assert argv[0:2] in (["/bin/launchctl", "bootout"], ["/bin/launchctl", "bootstrap"])
        action = argv[1]
        events.append(action)
        service["loaded"] = action == "bootstrap"
        return SimpleNamespace(returncode=0)

    def fake_check_output(argv, **kwargs):
        assert argv == ["/bin/launchctl", "list"]
        return f"123\t0\t{label}\n"

    def parsed_but_stale_config(*args, **kwargs):
        bindings, options = real_load_config(*args, **kwargs)
        loads.append(bindings)
        binding = bindings["claude_code"]
        if mismatch == "target":
            binding.target = {"session_id": world.old_id, "workspace": world.old_workspace}
        else:
            binding.adapter.command = ["/tmp/different-bridge"]
        return bindings, options

    monkeypatch.setattr(conversation_deployment, "listener", fake_listener)
    monkeypatch.setattr(conversation_deployment.subprocess, "run", fake_run)
    monkeypatch.setattr(conversation_deployment.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(conversation_deployment.time, "sleep", lambda _: None)
    monkeypatch.setattr(onboarding, "load_config", parsed_but_stale_config)

    with pytest.raises(RuntimeError, match="Installed Claude binding differs"):
        onboarding.install(world.root, world.data, originals, replacements, world.metadata,
            world.desktop_id, world.cli_id, world.workspace, world.record, plist_path,
            replace_binding=digest)

    assert len(loads) == 1
    assert events == ["bootout", "bootstrap", "bootout", "bootstrap"]
    assert service["loaded"] is True
    assert {path: path.read_bytes() for path in before} == before
