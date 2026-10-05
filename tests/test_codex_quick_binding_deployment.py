import importlib.util
import json
import os
from pathlib import Path

import pytest

from test_native_conversation_deployment import ROOT, deployment as ingress_deployment
from test_native_conversation_deployment import installation as native_installation


spec = importlib.util.spec_from_file_location(
    'quick_binding_deployment', ROOT / 'integrations/codex_desktop/quick_binding_deployment.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


@pytest.fixture
def installation(tmp_path):
    data, project, _ = native_installation.__wrapped__(tmp_path)
    _, ingress = ingress_deployment.prepare(ROOT, data, 'codex_desktop')
    for path, content in ingress.items():
        path.write_bytes(content)

    server_path = data / 'server.json'
    server = json.loads(server_path.read_text())
    server['peers'].update({'claude_code': 'claude-token', 'gemini': 'gemini-token'})
    server['approval_tokens'].update({'claude_code': 'claude-approval', 'gemini': 'gemini-approval'})
    server_path.write_text(json.dumps(server, indent=2) + '\n')

    claude_endpoint = data / 'claude_code.json'
    claude_endpoint.write_text(json.dumps({
        'peer': 'claude_code', 'client': 'generic', 'tool_profile': 'participant',
        'mcp_locale': 'en-US', 'approval_transport': 'host_tool_permission',
        'token': 'claude-token', 'approval_token': 'claude-approval',
        'unrelated_endpoint_field': {'keep': True},
    }, indent=2) + '\n')
    gemini_endpoint = data / 'gemini.json'
    gemini_endpoint.write_text(json.dumps({
        'peer': 'gemini', 'client': 'generic', 'tool_profile': 'participant',
        'mcp_locale': 'en-US', 'approval_transport': 'elicitation',
        'token': 'gemini-token', 'approval_token': 'gemini-approval',
        'unrelated_endpoint_field': {'keep': True},
    }, indent=2) + '\n')

    claude_config = tmp_path / 'claude_desktop_config.json'
    claude_prefix = ['-E', '-B', '-m', 'local_ai_connector.cli', 'mcp',
                     '--config', str(claude_endpoint), '--claude-binding-root', str(ROOT)]
    claude_config.write_text(json.dumps({
        'approvalPolicy': 'ask',
        'mcpServers': {
            'local_ai_connector': {'command': str(previous_claude_python(tmp_path)),
                                   'args': claude_prefix},
            'other_server': {'command': '/usr/bin/true', 'args': ['keep'], 'env': {'A': 'B'}},
        },
    }, indent=2) + '\n')

    antigravity_config = tmp_path / 'gemini_settings.json'
    gemini_config = {
        'approvalPolicy': 'ask',
        'mcpServers': {
            'local_ai_connector': {
                'command': str(ROOT / '.venv/bin/python'),
                'args': ['-m', 'local_ai_connector.cli', 'mcp', '--config', str(gemini_endpoint)],
                'env': {'PYTHONPATH': str(ROOT / 'src'), 'PRESERVE': 'yes'},
                'disabledTools': ['connector_archive'],
            },
            'other_server': {'command': '/usr/bin/true', 'args': ['keep']},
        },
    }
    antigravity_config.write_text(json.dumps(gemini_config, indent=2) + '\n')

    codex_cli = executable(tmp_path / 'bin/codex')
    candidate_python = executable(tmp_path / 'bin/candidate-python')
    old_claude_python = previous_claude_python(tmp_path)
    return {
        'data': data, 'project': project, 'claude_config': claude_config,
        'antigravity_config': antigravity_config, 'codex_cli': codex_cli,
        'candidate_python': candidate_python, 'previous_claude_python': old_claude_python,
        'server_path': server_path, 'claude_endpoint': claude_endpoint,
        'gemini_endpoint': gemini_endpoint,
    }


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


def previous_claude_python(directory):
    return executable(directory / 'bin/previous-claude-python')


def prepare(installation, antigravity_project=None):
    return deployment.prepare(
        ROOT, installation['data'], installation['claude_config'],
        installation['antigravity_config'], installation['codex_cli'],
        installation['candidate_python'], installation['previous_claude_python'],
        antigravity_project=antigravity_project)


def add_antigravity_project(installation, tmp_path):
    project_id = '55555555-5555-4555-8555-555555555555'
    wake_path = installation['data'] / 'wakeup.json'
    wake = json.loads(wake_path.read_text())
    wake['bindings']['gemini'] = {'target': {'project_id': project_id}}
    wake_path.write_text(json.dumps(wake, indent=2) + '\n')
    project = tmp_path / 'antigravity_project.json'
    project.write_text(json.dumps({
        'id': project_id,
        'projectSetting': {'preserve': True},
        'permissionGrants': {
            'v2Migrated': True,
            'otherWrapperField': 'preserve',
            'permissionGrants': {
                'allow': ['mcp(other/keep)', 'mcp(local_ai_connector/connector_codex_binding_catalog)'],
                'ask': ['mcp(other/ask)'],
                'deny': ['mcp(other/deny)'],
                'otherGrantField': ['retain'],
            },
        },
    }, indent=2) + '\n')
    return project


def test_prepare_changes_only_server_cli_and_two_existing_launchers_idempotently(installation):
    paths = [installation['server_path'], installation['claude_config'],
             installation['antigravity_config']]
    originals, replacements = prepare(installation)

    assert set(replacements) == set(paths)
    assert originals[installation['data'] / 'wakeup.json'] == (installation['data'] / 'wakeup.json').read_bytes()
    assert originals[installation['data'] / 'codex_desktop.json'] == (installation['data'] / 'codex_desktop.json').read_bytes()
    before_server = json.loads(originals[installation['server_path']])
    after_server = json.loads(replacements[installation['server_path']])
    expected_server = dict(before_server, codex_binding_cli=str(installation['codex_cli']))
    assert after_server == expected_server
    assert after_server['peers']['claude_code'] == 'claude-token'
    assert after_server['peers']['gemini'] == 'gemini-token'
    assert after_server['approval_tokens']['claude_code'] == 'claude-approval'
    assert after_server['approval_tokens']['gemini'] == 'gemini-approval'
    assert after_server['peers']['other'] == 'preserve'
    assert after_server['approval_tokens']['other'] == 'preserve-approval'

    claude_before = json.loads(originals[installation['claude_config']])
    claude_after = json.loads(replacements[installation['claude_config']])
    claude_entry = claude_after['mcpServers']['local_ai_connector']
    assert claude_entry['command'] == str(installation['candidate_python'])
    assert claude_entry['args'] == [
        '-E', '-B', '-m', 'local_ai_connector.cli', 'mcp', '--config',
        str(installation['claude_endpoint']), '--claude-binding-root', str(ROOT),
        '--codex-quick-binding']
    claude_before['mcpServers'].pop('local_ai_connector')
    claude_after['mcpServers'].pop('local_ai_connector')
    assert claude_after == claude_before

    gemini_before = json.loads(originals[installation['antigravity_config']])
    gemini_after = json.loads(replacements[installation['antigravity_config']])
    gemini_entry = gemini_after['mcpServers']['local_ai_connector']
    assert gemini_entry['command'] == str(ROOT / '.venv/bin/python')
    assert gemini_entry['args'] == [
        '-m', 'local_ai_connector.cli', 'mcp', '--config', str(installation['gemini_endpoint']),
        '--codex-quick-binding']
    gemini_before['mcpServers'].pop('local_ai_connector')
    gemini_after['mcpServers'].pop('local_ai_connector')
    assert gemini_after == gemini_before

    untouched = [installation['claude_endpoint'], installation['gemini_endpoint'],
                 installation['data'] / 'codex_desktop.json',
                 installation['data'] / 'wakeup.json',
                 installation['project'] / '.codex/hooks.json',
                 installation['project'] / '.codex/config.toml']
    saved = {path: path.read_bytes() for path in untouched}
    for path, content in replacements.items():
        path.write_bytes(content)
    again_originals, again = prepare(installation)
    assert set(again_originals) == set(originals)
    assert again == replacements
    assert {path: path.read_bytes() for path in untouched} == saved


@pytest.mark.parametrize('filename', ['wakeup.json', 'codex_desktop.json'])
def test_prepare_refuses_guard_changes_during_ingress_validation(installation, monkeypatch, filename):
    original = deployment.ingress_configuration

    def racing_ingress(*args):
        result = original(*args)
        path = installation['data'] / filename
        path.write_bytes(path.read_bytes() + b'\n')
        return result

    monkeypatch.setattr(deployment, 'ingress_configuration', racing_ingress)
    with pytest.raises(ValueError, match='changed during preparation'):
        prepare(installation)


def test_prepare_refuses_when_existing_trusted_ingress_hooks_need_setup(installation):
    hooks = installation['project'] / '.codex/hooks.json'
    hooks.write_text('{"hooks": {}}\n')

    with pytest.raises(ValueError, match='Hooks differ|Install and trust the existing Codex ingress'):
        prepare(installation)


def test_prepare_refuses_conflicting_codex_metadata_cli(installation):
    server_path = installation['server_path']
    server = json.loads(server_path.read_text())
    server['codex_binding_cli'] = '/elsewhere/codex'
    server_path.write_text(json.dumps(server, indent=2) + '\n')

    with pytest.raises(ValueError, match='Another Codex metadata source'):
        prepare(installation)


@pytest.mark.parametrize('field,value', [
    ('peer', 'different-peer'),
    ('token', 'wrong-token'),
    ('approval_token', 'wrong-approval'),
    ('approval_transport', 'elicitation'),
])
def test_prepare_refuses_initiating_endpoint_identity_or_approval_mismatch(installation, field, value):
    endpoint_path = installation['data'] / 'claude_code.json'
    endpoint = json.loads(endpoint_path.read_text())
    endpoint[field] = value
    endpoint_path.write_text(json.dumps(endpoint))

    with pytest.raises(ValueError, match='initiating endpoint identity or approval'):
        prepare(installation)


@pytest.mark.parametrize('mutation', ['command', 'args'])
def test_prepare_refuses_a_foreign_claude_launcher(installation, mutation):
    config_path = installation['claude_config']
    config = json.loads(config_path.read_text())
    entry = config['mcpServers']['local_ai_connector']
    entry[mutation] = '/foreign/program' if mutation == 'command' else ['-m', 'foreign.server']
    config_path.write_text(json.dumps(config))

    with pytest.raises(ValueError, match='launcher differs'):
        prepare(installation)


def test_prepare_refuses_symlinked_configuration(installation, tmp_path):
    config_path = installation['antigravity_config']
    target = tmp_path / 'real-gemini-settings.json'
    target.write_bytes(config_path.read_bytes())
    config_path.unlink()
    config_path.symlink_to(target)

    with pytest.raises(ValueError, match='owner-controlled regular configuration'):
        prepare(installation)


@pytest.mark.parametrize('restriction', ['disabled', 'required_tool'])
def test_prepare_refuses_disabled_launcher_or_required_tool(installation, restriction):
    config_path = installation['antigravity_config']
    config = json.loads(config_path.read_text())
    entry = config['mcpServers']['local_ai_connector']
    if restriction == 'disabled':
        entry['disabled'] = True
    else:
        entry['disabledTools'].append('connector_codex_quick_bind')
    config_path.write_text(json.dumps(config))

    with pytest.raises(ValueError, match='launcher differs|tool restriction conflicts'):
        prepare(installation)


def test_optional_project_grants_are_exact_and_preserve_existing_policy(installation, tmp_path):
    project = add_antigravity_project(installation, tmp_path)
    originals, replacements = prepare(installation, project)

    assert set(replacements) == {
        installation['server_path'], installation['claude_config'],
        installation['antigravity_config'], project,
    }
    before = json.loads(originals[project])
    after = json.loads(replacements[project])
    old_grants = before['permissionGrants']['permissionGrants']
    grants = after['permissionGrants']['permissionGrants']
    requested = [
        'mcp(local_ai_connector/connector_codex_binding_catalog)',
        'mcp(local_ai_connector/connector_codex_bound_chat)',
        'mcp(local_ai_connector/connector_codex_bind_chat)',
        'mcp(local_ai_connector/connector_codex_quick_bind)',
    ]
    assert grants['allow'] == [*old_grants['allow'], *requested[1:]]
    assert grants['ask'] == old_grants['ask']
    assert grants['deny'] == old_grants['deny']
    assert grants['otherGrantField'] == old_grants['otherGrantField']
    assert after['permissionGrants']['otherWrapperField'] == 'preserve'
    assert after['projectSetting'] == before['projectSetting']
    endpoint = json.loads(installation['gemini_endpoint'].read_text())
    assert endpoint['approval_transport'] == 'elicitation'
    assert installation['gemini_endpoint'] not in replacements

    for path, content in replacements.items():
        path.write_bytes(content)
    _, repeated = prepare(installation, project)
    assert repeated == replacements


@pytest.mark.parametrize(('bucket', 'grant'), [
    ('ask', 'mcp(local_ai_connector/*)'),
    ('deny', 'mcp(local_ai_connector/connector_codex_quick_bind)'),
])
def test_optional_project_grant_conflicts_are_refused_without_writes(
        installation, tmp_path, bucket, grant):
    project = add_antigravity_project(installation, tmp_path)
    document = json.loads(project.read_text())
    document['permissionGrants']['permissionGrants'][bucket].append(grant)
    project.write_text(json.dumps(document, indent=2) + '\n')
    before = {path: path.read_bytes() for path in [
        installation['server_path'], installation['claude_config'],
        installation['antigravity_config'], project,
    ]}

    with pytest.raises(ValueError, match='Antigravity (ask|deny) contains a matching grant'):
        prepare(installation, project)

    assert {path: path.read_bytes() for path in before} == before


def test_optional_project_must_match_registered_project_id(installation, tmp_path):
    project = add_antigravity_project(installation, tmp_path)
    document = json.loads(project.read_text())
    document['id'] = '66666666-6666-4666-8666-666666666666'
    project.write_text(json.dumps(document, indent=2) + '\n')

    with pytest.raises(ValueError, match='currently registered Antigravity project'):
        prepare(installation, project)
