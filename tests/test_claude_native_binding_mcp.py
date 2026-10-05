import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from mcp import Client

from local_ai_connector.mcp_server import create_mcp
from test_claude_guided_binding import isolated
from test_claude_onboarding import open_inbox, world
from test_claude_desktop_binding import _bind_selected_to_existing_record


def _dump(result):
    return json.loads(result.content[0].text)


def _files(path):
    return {item.relative_to(path).as_posix(): item.read_bytes()
            for item in path.rglob('*') if item.is_file()}


async def _tool_names(server):
    async with Client(server, mode='legacy') as client:
        return {tool.name for tool in (await client.list_tools()).tools}


async def test_binding_tools_are_opt_in_and_have_only_two_extra_readonly_tools():
    async def unused_invoke(ctx, **payload):
        raise AssertionError('binding tools must not route through broker.invoke')

    plain = create_mcp(unused_invoke)
    async def catalog():
        return {'schema_version': 1, 'status': 'catalog_ready', 'choices': [],
                'current': 'none', 'unavailable': [], 'binding_commit_supported': False}
    async def inspect(desktop_id, expected_revision):
        return {'status': 'native_authorization_unavailable', 'changes': [],
                'commit_supported': False}

    enabled = create_mcp(unused_invoke, binding_catalog=catalog, binding_inspect=inspect)
    ordinary = await _tool_names(plain)
    opted_in = await _tool_names(enabled)
    assert 'connector_claude_binding_catalog' not in ordinary
    assert 'connector_claude_binding_inspect' not in ordinary
    assert opted_in - ordinary == {
        'connector_claude_binding_catalog', 'connector_claude_binding_inspect'}


def test_binding_mcp_requires_both_callbacks():
    async def callback(*args):
        return {}
    with pytest.raises(ValueError, match='both .* together'):
        create_mcp(callback, binding_catalog=callback)
    with pytest.raises(ValueError, match='both .* together'):
        create_mcp(callback, binding_inspect=callback)


async def test_binding_mcp_schema_is_minimal_and_callbacks_are_direct():
    calls = []
    catalog_value = {'schema_version': 1, 'status': 'catalog_ready', 'choices': [],
                     'current': 'none', 'unavailable': [], 'binding_commit_supported': False}
    inspect_value = {'status': 'existing_binding_verified', 'changes': [],
                     'commit_supported': False}

    async def invoke(ctx, **payload):
        raise AssertionError('binding tools must not call the broker')
    async def catalog():
        calls.append(('catalog',))
        return catalog_value
    async def inspect(desktop_id, expected_revision):
        calls.append(('inspect', desktop_id, expected_revision))
        return inspect_value

    async with Client(create_mcp(invoke, binding_catalog=catalog,
                                 binding_inspect=inspect), mode='legacy') as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ('connector_claude_binding_catalog',
                     'connector_claude_binding_inspect'):
            annotations = tools[name].annotations
            assert annotations.read_only_hint is True
            assert annotations.destructive_hint is False
            if hasattr(annotations, 'idempotent_hint'):
                assert annotations.idempotent_hint is True
        schema = tools['connector_claude_binding_inspect'].input_schema
        assert set(schema['properties']) == {'desktop_id', 'expected_revision'}
        assert set(schema.get('required', ())) == {'desktop_id', 'expected_revision'}
        forbidden = {'path', 'root', 'home', 'peer', 'socket', 'socket_path',
                     'apply', 'approval', 'approve', 'token'}
        assert not forbidden.intersection(schema['properties'])

        got_catalog = await client.call_tool('connector_claude_binding_catalog', {})
        got_inspect = await client.call_tool('connector_claude_binding_inspect', {
            'desktop_id': 'local_selected', 'expected_revision': 'revision-1'})
    assert not got_catalog.is_error and _dump(got_catalog) == catalog_value
    assert not got_inspect.is_error and _dump(got_inspect) == inspect_value
    assert calls == [('catalog',), ('inspect', 'local_selected', 'revision-1')]


async def test_binding_callback_errors_are_redacted():
    secret = '/private/path/peer-secret/socket.sock'
    async def invoke(ctx, **payload):
        raise AssertionError('must not call broker')
    async def catalog():
        raise ValueError(secret)
    async def inspect(desktop_id, expected_revision):
        raise ValueError(secret)

    async with Client(create_mcp(invoke, binding_catalog=catalog,
                                 binding_inspect=inspect), mode='legacy') as client:
        for name, arguments in (
                ('connector_claude_binding_catalog', {}),
                ('connector_claude_binding_inspect', {
                    'desktop_id': 'local_selected', 'expected_revision': 'r1'})):
            result = await client.call_tool(name, arguments)
            assert result.is_error
            assert secret not in result.content[0].text


def _add_session(setup, *, title, desktop_id=None, cli_id=None, workspace=None,
                 archived=False):
    desktop_id = desktop_id or 'local_' + str(uuid4())
    cli_id = cli_id or str(uuid4())
    workspace = workspace or setup.home / ('Workspace ' + str(uuid4()))
    workspace.mkdir(parents=True, exist_ok=True)
    metadata = setup.sessions / 'profile-extra' / 'project-extra' / (desktop_id + '.json')
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps({
        'sessionId': desktop_id, 'cliSessionId': cli_id, 'cwd': str(workspace),
        'originCwd': str(workspace), 'isArchived': archived, 'title': title,
    }))
    metadata.chmod(0o600)
    return desktop_id, cli_id, metadata, workspace


def _choice(result, desktop_id):
    return next(row for row in result['choices'] if row['desktop_id'] == desktop_id)


def test_package_catalog_revision_is_stable_when_unrelated_rows_reorder_or_appear(
        world, tmp_path, monkeypatch):
    from local_ai_connector import claude_binding
    setup = isolated(world, tmp_path, monkeypatch)
    before_data = _files(setup.data)
    before_records = _files(world.record_dir)
    duplicate = _add_session(setup, title='Same title', workspace=setup.workspace.parent / 'other')
    # A second identical display title must still be separately addressable.
    obj = json.loads(duplicate[2].read_text())
    obj['title'] = 'Selected Claude chat'
    duplicate[2].write_text(json.dumps(obj))

    first = claude_binding.catalog(setup.data, setup.home)
    selected = _choice(first, setup.desktop_id)
    assert first['schema_version'] == 1 and first['status'] == 'catalog_ready'
    assert first['binding_commit_supported'] is False
    assert isinstance(selected['selection_revision'], str) and selected['selection_revision']
    assert sum(choice['title'] == selected['title'] for choice in first['choices']) == 2

    _add_session(setup, title='An unrelated later row')
    second = claude_binding.catalog(setup.data, setup.home)
    assert _choice(second, setup.desktop_id)['selection_revision'] == selected['selection_revision']
    serialized = json.dumps(first, ensure_ascii=False)
    for secret in (setup.cli_id, 'test-peer-token', 'test-admin-token',
                   str(world.inbox_path), str(setup.metadata)):
        assert secret not in serialized
    assert _files(setup.data) == before_data
    assert _files(world.record_dir) == before_records


def test_package_inspect_rejects_stale_revision_before_private_record_or_socket_inspection(
        world, tmp_path, monkeypatch):
    from local_ai_connector import claude_binding
    setup = isolated(world, tmp_path, monkeypatch)
    catalog = claude_binding.catalog(setup.data, setup.home)
    revision = _choice(catalog, setup.desktop_id)['selection_revision']
    setup.metadata.write_text(json.dumps({
        'sessionId': setup.desktop_id, 'cliSessionId': setup.cli_id,
        'cwd': str(setup.workspace), 'originCwd': str(setup.workspace),
        'isArchived': False, 'title': 'Renamed after catalog',
    }))
    before_data = _files(setup.data)
    before_record = _files(world.record_dir)
    def forbidden(*args, **kwargs):
        pytest.fail('stale catalog revisions must be rejected before record or socket inspection')
    monkeypatch.setattr(claude_binding, 'confirmed_session', forbidden)
    monkeypatch.setattr(claude_binding, 'socket_identity', forbidden)

    result = claude_binding.inspect(setup.root, setup.data, setup.home,
                                    setup.desktop_id, revision)

    assert result['status'] == 'catalog_stale'
    assert _files(setup.data) == before_data
    assert _files(world.record_dir) == before_record
    assert not any(name.endswith('.lock') for name in _files(setup.data))


@pytest.mark.parametrize('drift', ['title', 'workspace', 'cli', 'archive', 'binding'])
def test_package_inspect_revision_pins_selected_metadata_and_binding(
        world, tmp_path, monkeypatch, drift):
    from local_ai_connector import claude_binding
    setup = isolated(world, tmp_path, monkeypatch)
    catalog = claude_binding.catalog(setup.data, setup.home)
    revision = _choice(catalog, setup.desktop_id)['selection_revision']
    metadata = json.loads(setup.metadata.read_text())
    if drift == 'title':
        metadata['title'] = 'A new title'
    elif drift == 'workspace':
        other = setup.home / 'Changed workspace'
        other.mkdir()
        metadata['cwd'] = metadata['originCwd'] = str(other)
    elif drift == 'cli':
        metadata['cliSessionId'] = str(uuid4())
    elif drift == 'archive':
        metadata['isArchived'] = True
    elif drift == 'binding':
        wake_path = setup.data / 'wakeup.json'
        wake = json.loads(wake_path.read_text())
        wake['bindings']['claude_code']['target']['workspace'] = str(setup.home)
        wake_path.write_text(json.dumps(wake))
    if drift != 'binding':
        setup.metadata.write_text(json.dumps(metadata))

    result = claude_binding.inspect(setup.root, setup.data, setup.home,
                                    setup.desktop_id, revision)
    assert result['status'] == 'catalog_stale'
    assert result['changes'] == []
    assert result['binding_commit_supported'] is False


def test_package_inspect_reports_safe_readonly_outcomes(world, tmp_path, monkeypatch):
    from local_ai_connector import claude_binding
    setup = isolated(world, tmp_path, monkeypatch)
    catalog = claude_binding.catalog(setup.data, setup.home)
    revision = _choice(catalog, setup.desktop_id)['selection_revision']
    before = _files(setup.data)
    before_records = _files(world.record_dir)

    result = claude_binding.inspect(setup.root, setup.data, setup.home,
                                    setup.desktop_id, revision)

    assert result['status'] == 'native_authorization_unavailable'
    assert result['changes'] == []
    assert result['binding_commit_supported'] is False
    assert _files(setup.data) == before
    assert _files(world.record_dir) == before_records
    assert not any(name.endswith('.lock') for name in _files(setup.data))
    serialized = json.dumps(result)
    for secret in (setup.cli_id, 'test-peer-token', 'test-admin-token',
                   str(world.inbox_path), str(setup.metadata)):
        assert secret not in serialized


def _revise_binding(setup, *, workspace):
    path = setup.data / 'wakeup.json'
    wake = json.loads(path.read_text())
    wake['bindings']['claude_code']['target']['workspace'] = str(workspace)
    path.write_text(json.dumps(wake))


def test_scan_time_binding_change_returns_stale_before_private_validation(
        world, tmp_path, monkeypatch):
    from local_ai_connector import claude_binding
    setup = isolated(world, tmp_path, monkeypatch)
    _bind_selected_to_existing_record(setup)
    revision = _choice(claude_binding.catalog(setup.data, setup.home),
                       setup.desktop_id)['selection_revision']
    original_choices = claude_binding._choices

    def change_during_scan(home):
        result = original_choices(home)
        _revise_binding(setup, workspace=setup.home)
        return result

    def forbidden(*args, **kwargs):
        pytest.fail('a scan-time binding change must be rejected before private validation')

    monkeypatch.setattr(claude_binding, '_choices', change_during_scan)
    monkeypatch.setattr(claude_binding, 'confirmed_session', forbidden)
    monkeypatch.setattr(claude_binding, 'socket_identity', forbidden)

    result = claude_binding.inspect(setup.root, setup.data, setup.home,
                                    setup.desktop_id, revision)

    assert result['status'] == 'catalog_stale'


@pytest.mark.parametrize('drift', ['binding', 'wakeup_enabled', 'send_when_unknown'])
def test_state_change_during_private_validation_never_returns_verified(
        world, tmp_path, monkeypatch, drift):
    from local_ai_connector import claude_binding
    setup = isolated(world, tmp_path, monkeypatch)
    _bind_selected_to_existing_record(setup)
    revision = _choice(claude_binding.catalog(setup.data, setup.home),
                       setup.desktop_id)['selection_revision']
    original_confirm = claude_binding.confirmed_session
    validated = []

    def validate_then_change(target, record):
        result = original_confirm(target, record)
        validated.append(result)
        if drift == 'binding':
            _revise_binding(setup, workspace=setup.home)
        else:
            path = setup.data / 'wakeup.json'
            wake = json.loads(path.read_text())
            if drift == 'wakeup_enabled':
                wake['enabled'] = False
            else:
                wake['send_when_unknown'] = False
            path.write_text(json.dumps(wake))
        return result

    monkeypatch.setattr(claude_binding, 'confirmed_session', validate_then_change)

    result = claude_binding.inspect(setup.root, setup.data, setup.home,
                                    setup.desktop_id, revision)

    assert len(validated) == 1
    assert result['status'] == 'catalog_stale'
    assert result['status'] != 'existing_binding_verified'


@pytest.mark.parametrize('script', ['native_preflight.py', 'bridge.py'])
def test_standalone_claude_scripts_help_without_pythonpath(script, tmp_path):
    project = Path(__file__).resolve().parents[1]
    path = project / 'integrations/claude_code' / script
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)

    result = subprocess.run([sys.executable, str(path), '--help'], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout.lower()
