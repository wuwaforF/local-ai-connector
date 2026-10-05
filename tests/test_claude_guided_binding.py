import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

from test_claude_onboarding import world, open_inbox, read_json, write_metadata
from integrations.claude_code import guided_binding, onboarding

pytestmark = pytest.mark.macos  # macOS deployment scripts, launchd or Desktop paths


def isolated(world, tmp_path, monkeypatch, *, first_binding=False, workspace_name="Project ü space"):
    home = tmp_path / "second user's home ü"
    home.mkdir()
    data = home / ".local/share/local-ai-connector"
    data.mkdir(parents=True)
    root = tmp_path / "connector runtime space"
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("synthetic runtime placeholder")
    (root / "integrations/claude_code").mkdir(parents=True)
    bridge = root / "integrations/claude_code/bridge.py"
    bridge.write_text("synthetic bridge placeholder")

    shutil.copyfile(world.data / "server.json", data / "server.json")
    shutil.copyfile(world.data / "claude_code.json", data / "claude_code.json")
    shutil.copyfile(world.data / "state.sqlite3", data / "state.sqlite3")
    wake = read_json(world.data / "wakeup.json")
    old_record = data / (world.old_id + ".json")
    if first_binding:
        wake["bindings"].pop("claude_code")
    else:
        old_record.write_text(json.dumps({"session_id": world.old_id, "workspace": world.old_workspace,
                                          "sentinel": "old session record"}))
        old_record.chmod(0o600)
        wake["bindings"]["claude_code"]["command"] = [
            str(python), str(bridge), "--session-record", str(old_record)]
    (data / "wakeup.json").write_text(json.dumps(wake))
    for path in data.iterdir():
        if path.is_file():
            path.chmod(0o600)

    workspace = home / "Workspaces" / workspace_name
    workspace.mkdir(parents=True)
    desktop_id = "local_" + str(uuid4())
    cli_id = str(uuid4())
    sessions = home / "Library/Application Support/Claude/claude-code-sessions"
    metadata = sessions / "profile-1" / "project-1" / (desktop_id + ".json")
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({
        "sessionId": desktop_id,
        "cliSessionId": cli_id,
        "cwd": str(workspace),
        "originCwd": str(workspace),
        "isArchived": False,
        "title": "Selected Claude chat",
    }))
    metadata.chmod(0o600)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    selected = guided_binding.candidates(sessions)[0]
    receipt = {
        "schema": 1,
        "nonce": "f" * 32,
        "cli_session_id": cli_id,
        "workspace": str(workspace),
        "messaging_socket": str(world.inbox_path),
    }
    return type("Setup", (), {
        "home": home, "data": data, "root": root, "python": python, "bridge": bridge,
        "workspace": workspace, "desktop_id": desktop_id, "cli_id": cli_id,
        "sessions": sessions, "metadata": metadata, "selected": selected,
        "receipt": receipt, "old_record": old_record,
    })()


def test_candidates_return_safe_titles_and_explicitly_report_unavailable_worktrees(world, tmp_path):
    home = tmp_path / "metadata home"
    directory = home / "Library/Application Support/Claude/claude-code-sessions"
    good_dir, worktree_dir, no_engine_dir = (directory / "p" / name for name in ("good", "worktree", "no-engine"))
    good_dir.mkdir(parents=True)
    worktree_dir.mkdir(parents=True)
    no_engine_dir.mkdir(parents=True)
    selected_id, worktree_id, no_engine_id = ("local_" + str(uuid4()) for _ in range(3))
    cli_id = str(uuid4())
    workspace = tmp_path / "selected workspace"
    workspace.mkdir()
    records = [
        (good_dir / (selected_id + ".json"), {"sessionId": selected_id, "cliSessionId": cli_id,
         "cwd": str(workspace), "originCwd": str(workspace), "isArchived": False,
         "title": "\u001b[31mClaude\nselected\u0000 chat"}),
        (worktree_dir / (worktree_id + ".json"), {"sessionId": worktree_id, "cliSessionId": str(uuid4()),
         "cwd": str(workspace), "originCwd": str(tmp_path), "isArchived": False, "title": "worktree"}),
        (no_engine_dir / (no_engine_id + ".json"), {"sessionId": no_engine_id, "cwd": str(workspace),
         "originCwd": str(workspace), "isArchived": False, "title": "warming up"}),
    ]
    for path, value in records:
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    unavailable = []
    candidates = guided_binding.candidates(directory, unavailable=unavailable)
    assert len(candidates) == 1
    assert candidates[0]["identity"]["sessionId"] == selected_id
    assert "\x1b" not in candidates[0]["title"]
    assert "\n" not in candidates[0]["title"] and "\0" not in candidates[0]["title"]
    assert any("worktree" in message for message in unavailable)
    assert any("warming up" in message for message in unavailable)


def test_duplicate_desktop_ids_are_rejected_instead_of_choosing_a_file(tmp_path):
    directory = tmp_path / "sessions"
    desktop_id, cli_id = "local_" + str(uuid4()), str(uuid4())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = {"sessionId": desktop_id, "cliSessionId": cli_id, "cwd": str(workspace),
               "originCwd": str(workspace), "isArchived": False}
    for profile in ("profile-a", "profile-b"):
        path = directory / profile / "project" / (desktop_id + ".json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
    with pytest.raises(ValueError, match="Ambiguous Desktop identity"):
        guided_binding.candidates(directory)


def test_single_candidate_still_requires_explicit_picker_choice_and_cancel_is_read_only(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in setup.data.iterdir() if path.is_file()}
    output = []
    prompts = []
    assert guided_binding.run(root=setup.root, home=setup.home, read=lambda prompt: prompts.append(prompt) or "",
                             write=output.append) is None
    assert any("1." in line and setup.desktop_id in line for line in output)
    assert prompts == ["输入目标聊天编号（回车取消）："]
    assert not any("复制到这个 Claude 聊天" in line for line in output)
    assert not (setup.data / (setup.cli_id + ".json")).exists()
    assert {path: path.read_bytes() for path in before} == before
    assert not (setup.workspace / ".mcp.json").exists()


def test_cancelling_after_receipt_does_not_write_record_or_configuration(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in setup.data.iterdir() if path.is_file()}
    output, prompts = [], []
    monkeypatch.setattr(guided_binding.secrets, "token_hex", lambda _: setup.receipt["nonce"])
    answers = iter(("1", json.dumps(setup.receipt), "n"))
    guided_binding.run(root=setup.root, home=setup.home,
        read=lambda prompt: prompts.append(prompt) or next(answers), write=output.append)
    assert prompts == ["输入目标聊天编号（回车取消）：", "粘贴 JSON 工具输出（单行，回车取消）：",
                       "确认绑定此聊天？[y/N]："]
    assert not (setup.data / (setup.cli_id + ".json")).exists()
    assert {path: path.read_bytes() for path in before} == before
    assert not (setup.workspace / ".mcp.json").exists()
    server = json.loads((setup.data / "server.json").read_bytes())
    tokens = [server["admin_token"], *server["peers"].values()]
    assert all(token not in "\n".join(output) for token in tokens)


def test_receipt_prompt_executes_against_current_chat_environment_values(world):
    nonce, cli_id, workspace, inbox = "ab" * 16, str(uuid4()), str(world.workspace), str(world.inbox_path)
    prompt = guided_binding.receipt_prompt(nonce)
    command = shlex.split(prompt.split("python3 -c ", 1)[1])[0]
    environment = os.environ.copy()
    environment.update(CLAUDE_CODE_SESSION_ID=cli_id, CLAUDE_CODE_MESSAGING_SOCKET=inbox)
    result = subprocess.run([sys.executable, "-c", command], cwd=workspace, env=environment,
                            check=True, text=True, capture_output=True, timeout=10)
    receipt = json.loads(result.stdout)
    assert receipt == {"schema": 1, "nonce": nonce, "cli_session_id": cli_id,
                       "workspace": str(Path(workspace).resolve()),
                       "messaging_socket": inbox}
    assert "CLAUDE_CODE_SESSION_ID" in command and "CLAUDE_CODE_MESSAGING_SOCKET" in command
    assert cli_id not in command


@pytest.mark.parametrize("mutation", ["wrong_nonce", "malformed", "duplicate", "extra", "missing", "wrong_session"])
def test_receipt_parser_rejects_untrusted_or_ambiguous_output(world, mutation):
    identity = {"cliSessionId": str(uuid4()), "cwd": str(world.workspace)}
    nonce = "12" * 16
    valid = {"schema": 1, "nonce": nonce, "cli_session_id": identity["cliSessionId"],
             "workspace": identity["cwd"], "messaging_socket": str(world.inbox_path)}
    text = json.dumps(valid)
    if mutation == "wrong_nonce":
        valid["nonce"] = "34" * 16
        text = json.dumps(valid)
    elif mutation == "malformed":
        text = "{" + text
    elif mutation == "duplicate":
        text = text[:-1] + ',"schema":1}'
    elif mutation == "extra":
        valid["token"] = "must-not-be-accepted"
        text = json.dumps(valid)
    elif mutation == "missing":
        del valid["messaging_socket"]
        text = json.dumps(valid)
    elif mutation == "wrong_session":
        valid["cli_session_id"] = str(uuid4())
        text = json.dumps(valid)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        guided_binding.validate_receipt(text, nonce, identity)


def test_receipt_accepts_distinct_desktop_and_cli_ids_and_project_mcp_preserves_unrelated_entries(
        world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    assert setup.desktop_id != setup.cli_id
    receipt = dict(setup.receipt)
    receipt["workspace"] = str(setup.workspace.resolve())
    validated = guided_binding.validate_receipt(json.dumps(receipt), receipt["nonce"], setup.selected["identity"])
    plan = guided_binding.preview(setup.root, setup.data, setup.selected, validated)
    mcp = json.loads((setup.workspace / ".mcp.json").read_bytes()) if (setup.workspace / ".mcp.json").exists() else None
    assert mcp is None
    mcp_path = setup.workspace / ".mcp.json"
    original = {"mcpServers": {"other": {"command": "/usr/bin/example", "args": ["--safe"]}}}
    mcp_path.write_text(json.dumps(original))
    path, before, proposed = guided_binding.project_mcp(setup.root, setup.data, setup.workspace)
    result = json.loads(proposed)
    assert path == mcp_path and before == mcp_path.read_bytes()
    assert result["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert result["mcpServers"]["local_ai_connector"]["command"] == str(setup.python)
    assert str(setup.home) in result["mcpServers"]["local_ai_connector"]["args"][-1]
    assert plan["report"]["target"] == {"session_id": setup.cli_id, "workspace": str(setup.workspace)}


def test_receipt_accepts_same_workspace_alias_but_rejects_another_directory(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    aliased = dict(setup.receipt, workspace=str(setup.workspace.resolve()))
    assert guided_binding.validate_receipt(json.dumps(aliased), aliased["nonce"], setup.selected["identity"])
    other = setup.home / "another real workspace"
    other.mkdir()
    wrong = dict(setup.receipt, workspace=str(other))
    with pytest.raises(ValueError, match="所选聊天不一致"):
        guided_binding.validate_receipt(json.dumps(wrong), wrong["nonce"], setup.selected["identity"])


def test_project_mcp_conflict_is_not_overwritten(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    path = setup.workspace / ".mcp.json"
    before = {"mcpServers": {"local_ai_connector": {"command": "/wrong/python", "args": ["wrong"]}}}
    path.write_text(json.dumps(before))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="已有不同的 local_ai_connector"):
        guided_binding.project_mcp(setup.root, setup.data, setup.workspace)
    assert path.read_bytes() == original


def test_first_binding_requires_explicit_creation_confirmation(world):
    wake_path = world.data / "wakeup.json"
    wake = read_json(wake_path)
    wake["bindings"].pop("claude_code")
    wake_path.write_text(json.dumps(wake))
    identity, target = onboarding.inspect_target(world.metadata, world.desktop_id, world.cli_id,
                                                 world.workspace, world.record)
    args = (world.root, world.data, identity, target, world.record)
    before = wake_path.read_bytes()
    with pytest.raises(ValueError, match="first Claude binding requires explicit create_binding"):
        onboarding.prepare_configuration(*args)
    originals, replacements, report = onboarding.prepare_configuration(*args, create_binding=True)
    assert report["first_binding"] is True
    assert report["previous_binding_sha256"] is None
    assert replacements[wake_path]
    assert wake_path.read_bytes() == before

    applied = []
    original_apply = onboarding.apply
    onboarding.apply = lambda *args, **kwargs: applied.append((args, kwargs))
    try:
        with pytest.raises(ValueError, match="first Claude binding requires explicit create_binding"):
            onboarding.install(world.root, world.data, originals, replacements, world.metadata,
                world.desktop_id, world.cli_id, world.workspace, world.record, world.base / "unused.plist")
        assert applied == []
        onboarding.install(world.root, world.data, originals, replacements, world.metadata,
            world.desktop_id, world.cli_id, world.workspace, world.record, world.base / "unused.plist",
            create_binding=True)
        assert len(applied) == 1
    finally:
        onboarding.apply = original_apply


def test_selected_title_change_is_benign_and_preview_still_uses_session_identity(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    write_metadata(setup, {"title": "Changed title after selection"})
    plan = guided_binding.preview(setup.root, setup.data, setup.selected, setup.receipt)
    assert plan["report"]["target"]["session_id"] == setup.cli_id


def test_configuration_race_after_preview_aborts_before_record_write(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    plan = guided_binding.preview(setup.root, setup.data, setup.selected, setup.receipt)
    wake_path = setup.data / "wakeup.json"
    wake = read_json(wake_path)
    wake["send_when_unknown"] = False
    wake_path.write_text(json.dumps(wake))
    current = wake_path.read_bytes()
    with pytest.raises(ValueError, match="Configuration changed after confirmation"):
        guided_binding.commit(plan, setup.home / "unused.plist")
    assert wake_path.read_bytes() == current
    assert not plan["record"].exists()


def test_preview_inode_race_is_detected_before_capture_is_written(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    plan = guided_binding.preview(setup.root, setup.data, setup.selected, setup.receipt)
    original_path = Path(setup.receipt["messaging_socket"])
    original_identity = (original_path.stat().st_dev, original_path.stat().st_ino)
    world.inbox_listener.close()
    original_path.unlink()
    replacement_listener, replacement_path = open_inbox(original_path.parent, original_path.name)
    try:
        assert (replacement_path.stat().st_dev, replacement_path.stat().st_ino) != original_identity
        with pytest.raises(ValueError, match="inbox changed after"):
            guided_binding.commit(plan, setup.home / "unused.plist")
        assert not plan["record"].exists()
    finally:
        replacement_listener.close()


def test_missing_chat_socket_after_preview_aborts_without_record_or_config_write(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    plan = guided_binding.preview(setup.root, setup.data, setup.selected, setup.receipt)
    wake_path = setup.data / "wakeup.json"
    original_wake = wake_path.read_bytes()
    world.inbox_listener.close()
    world.inbox_path.unlink()
    with pytest.raises(OSError):
        guided_binding.commit(plan, setup.home / "unused.plist")
    assert not plan["record"].exists()
    assert wake_path.read_bytes() == original_wake


def test_capture_write_identity_race_is_checked_against_preview_inode(world, tmp_path):
    record = tmp_path / (world.cli_id + ".json")
    before = record.read_bytes() if record.exists() else None
    original_info = world.inbox_path.stat()
    with pytest.raises(ValueError, match="Chat inbox changed after preview"):
        onboarding.capture(world.metadata, world.desktop_id, world.cli_id, world.workspace,
            record, world.inbox_path, write=True, expected_socket_identity=(original_info.st_dev, original_info.st_ino + 1))
    assert not record.exists()
    assert before is None


@pytest.mark.parametrize("previous_exists", [False, True])
def test_capture_post_replace_validation_failure_restores_or_removes_record(world, tmp_path, monkeypatch, previous_exists):
    record = world.record if previous_exists else tmp_path / (world.cli_id + ".json")
    original = record.read_bytes() if previous_exists else None
    monkeypatch.setattr(onboarding, "confirmed_session", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("post-write validation failed")))
    with pytest.raises(ValueError, match="post-write validation failed"):
        onboarding.capture(world.metadata, world.desktop_id, world.cli_id, world.workspace,
            record, world.inbox_path, write=True)
    if previous_exists:
        assert record.read_bytes() == original
    else:
        assert not record.exists()


def test_wrong_reviewed_digest_aborts_before_apply_and_rolls_back_capture(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    plan = guided_binding.preview(setup.root, setup.data, setup.selected, setup.receipt)
    plan["report"]["previous_binding_sha256"] = "0" * 64
    before = (setup.data / "wakeup.json").read_bytes()
    applied = []
    monkeypatch.setattr(onboarding, "apply", lambda *args, **kwargs: applied.append(True))
    with pytest.raises(ValueError, match="exact inspected previous binding digest"):
        guided_binding.commit(plan, setup.home / "unused.plist")
    assert applied == []
    assert (setup.data / "wakeup.json").read_bytes() == before
    assert not plan["record"].exists()


def test_concurrent_record_writer_is_preserved_when_install_fails(world, tmp_path, monkeypatch):
    setup = isolated(world, tmp_path, monkeypatch)
    plan = guided_binding.preview(setup.root, setup.data, setup.selected, setup.receipt)
    foreign = b'{"session_id":"concurrent-writer"}\n'

    def write_foreign_record(*args, **kwargs):
        plan["record"].write_bytes(foreign)
        raise RuntimeError("simulated install failure after concurrent write")

    monkeypatch.setattr(onboarding, "install", write_foreign_record)
    with pytest.raises(RuntimeError, match="changed concurrently"):
        guided_binding.commit(plan, setup.home / "unused.plist")
    assert plan["record"].read_bytes() == foreign


@pytest.mark.parametrize("first_binding", [True, False], ids=["first-binding", "replacement"])
def test_guided_yes_commits_first_binding_and_replacement_with_readiness(
        world, tmp_path, monkeypatch, first_binding):
    setup = isolated(world, tmp_path, monkeypatch, first_binding=first_binding)
    project_mcp = setup.workspace / ".mcp.json"
    original_project = {"mcpServers": {"unrelated": {"command": "/usr/bin/keep", "args": ["--keep"]}}}
    project_mcp.write_text(json.dumps(original_project))
    old_record_before = setup.old_record.read_bytes() if setup.old_record.exists() else None
    server_before = (setup.data / "server.json").read_bytes()
    endpoint_before = (setup.data / "claude_code.json").read_bytes()
    original_wake = read_json(setup.data / "wakeup.json")
    output, prompts, actions = [], [], []
    monkeypatch.setattr(guided_binding.secrets, "token_hex", lambda _: setup.receipt["nonce"])

    def apply_candidate(root, data, originals, replacements, plist_path, *, precommit=None, readiness=None):
        assert precommit is not None and readiness is not None
        precommit()
        actions.append((set(replacements), plist_path))
        for path, content in replacements.items():
            onboarding.replace(path, content)
        readiness()

    monkeypatch.setattr(onboarding, "apply", apply_candidate)
    answers = iter(("1", json.dumps(setup.receipt), "y"))
    guided_binding.run(root=setup.root, home=setup.home,
        read=lambda prompt: prompts.append(prompt) or next(answers), write=output.append)

    record = read_json(setup.data / (setup.cli_id + ".json"))
    assert record["session_id"] == setup.cli_id
    assert record["workspace"] == str(setup.workspace)
    assert record["desktop_id"] == setup.desktop_id
    assert record["socket"] == setup.receipt["messaging_socket"]
    assert record["socket_ino"] == Path(record["socket"]).stat().st_ino
    wake = read_json(setup.data / "wakeup.json")
    binding = wake["bindings"]["claude_code"]
    assert binding["target"] == {"session_id": setup.cli_id, "workspace": str(setup.workspace)}
    assert binding["command"] == [str(setup.python), str(setup.bridge), "--session-record", str(setup.data / (setup.cli_id + ".json"))]
    assert {peer: value for peer, value in wake["bindings"].items() if peer != "claude_code"} == {
        peer: value for peer, value in original_wake["bindings"].items() if peer != "claude_code"}
    assert wake["bindings"]["gemini"] == original_wake["bindings"]["gemini"]
    project = read_json(project_mcp)
    assert project["mcpServers"]["unrelated"] == original_project["mcpServers"]["unrelated"]
    assert project["mcpServers"]["local_ai_connector"]["command"] == str(setup.python)
    assert (setup.data / "server.json").read_bytes() == server_before
    assert (setup.data / "claude_code.json").read_bytes() == endpoint_before
    assert (setup.old_record.read_bytes() if setup.old_record.exists() else None) == old_record_before
    assert prompts.count("确认绑定此聊天？[y/N]：") == 1
    assert actions and len(actions) == 1
    changed_paths, plist_path = actions[0]
    assert changed_paths == {setup.data / "wakeup.json", project_mcp}
    assert plist_path == setup.home / "Library/LaunchAgents/dev.local-ai-connector.service.plist"
    assert any("绑定已保存" in line for line in output)
