import importlib.util
import json
from pathlib import Path
import subprocess
import tomllib

import pytest
from test_native_conversation_deployment import deployment as installer, installation

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ag_native_deployment', ROOT / 'integrations/antigravity/native_deployment.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)
PROJECT = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def ag_installation(installation, tmp_path):
    data, project, plist = installation
    home = tmp_path / 'home'
    for directory in (home / '.codex', home / '.gemini/config/sidecars', home / '.gemini/config/projects'):
        directory.mkdir(parents=True)
    server = json.loads((data / 'server.json').read_text())
    server['peers']['gemini'] = 'test-gemini'
    (data / 'server.json').write_text(json.dumps(server))
    (data / 'gemini.json').write_text(json.dumps({'peer': 'gemini', 'token': 'test-gemini', 'client': 'generic'}))
    wake = json.loads((data / 'wakeup.json').read_text())
    wake['bindings']['gemini'] = {'adapter': 'socket', 'socket': '/existing.sock', 'target': {
        'conversation_id': PROJECT, 'project_id': PROJECT, 'workspace_uri': project.as_uri()}}
    (data / 'wakeup.json').write_text(json.dumps(wake))
    (home / '.codex/config.toml').write_text(
        '[mcp_servers.local_ai_connector]\n'
        f'command = {json.dumps(str(ROOT / ".venv/bin/python"))}\n'
        f'args = {json.dumps(["-m", "local_ai_connector.cli", "mcp", "--config", str(data / "gpt.json")])}\n'
        'enabled_tools = ["connector_status", "connector_delegate", "connector_continue"]\n'
        '[mcp_servers.local_ai_connector.env]\n'
        f'PYTHONPATH = {json.dumps(str(ROOT / "src"))}\n')
    (home / '.gemini/config/config.json').write_text(json.dumps({'sidecars': {'existing': {'enabled': True}}, 'preserve': 7}))
    (home / '.gemini/config/projects' / (PROJECT + '.json')).write_text(json.dumps({
        'permissionGrants': {'v2Migrated': True, 'permissionGrants': {'allow': ['mcp(local_ai_connector/connector_receive)']}}}))
    return data, home, plist


def test_native_deployment_preserves_existing_routes_and_is_idempotent(ag_installation):
    data, home, _ = ag_installation
    wake = (data / 'wakeup.json').read_bytes()
    originals, replacements, target, socket = deployment.prepare(ROOT, data, home)
    assert len(replacements) == 5
    server = json.loads(replacements[data / 'server.json'])
    assert server['conversations']['gemini']['socket'] == str(socket)
    source = tomllib.loads(replacements[home / '.codex/config.toml'].decode())['mcp_servers']['local_ai_connector']
    assert source['enabled_tools'][-1] == 'connector_archive'
    assert source['tools']['connector_archive']['approval_mode'] == 'approve'
    for path, content in replacements.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    again, same, *_ = deployment.prepare(ROOT, data, home)
    assert again == same
    assert (data / 'wakeup.json').read_bytes() == wake
    assert json.loads((home / '.gemini/config/config.json').read_text())['sidecars']['existing'] == {'enabled': True}


@pytest.mark.parametrize('conflict', ['deny', 'registration', 'sidecar'])
def test_native_deployment_refuses_configuration_conflicts(ag_installation, conflict):
    data, home, _ = ag_installation
    if conflict == 'deny':
        path = home / '.gemini/config/projects' / (PROJECT + '.json')
        value = json.loads(path.read_text());value['permissionGrants']['permissionGrants']['deny'] = ['mcp(local_ai_connector/*)']
    elif conflict == 'registration':
        path = data / 'server.json'
        value = json.loads(path.read_text());value['conversations'] = {'gemini': {'provider': 'different'}}
    else:
        path = home / '.gemini/config/config.json'
        value = json.loads(path.read_text());value['sidecars'][deployment.SIDECAR] = {'enabled': False}
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError): deployment.prepare(ROOT, data, home)


def test_failed_native_readiness_restores_config_and_removes_new_sidecar(ag_installation, monkeypatch):
    data, home, plist = ag_installation
    originals, replacements, *_ = deployment.prepare(ROOT, data, home)
    state = {'pid': '101', 'loaded': True}
    def run(args, **kwargs):
        if args[1] == 'bootout': state['loaded'] = False
        elif args[1] == 'bootstrap': state.update(pid='102', loaded=True)
        else: raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(installer, 'listener', lambda *_: state['pid'] if state['loaded'] else None)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(subprocess, 'check_output', lambda *_args, **_kwargs: '101\t0\tdev.local-ai-connector.service\n')
    def readiness(): raise RuntimeError('host did not start')
    with pytest.raises(RuntimeError, match='host did not start'):
        installer.apply(ROOT, data, originals, replacements, plist, readiness=readiness)
    for path, content in originals.items():
        assert (path.read_bytes() if path.exists() else None) == content
    assert not (home / '.gemini/config/sidecars' / deployment.SIDECAR).exists()
    assert state['loaded']
