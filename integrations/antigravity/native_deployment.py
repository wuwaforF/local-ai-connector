"""Deploy the approved native-conversation provider and archive tool narrowly."""
import argparse
import asyncio
import fnmatch
import json
import os
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'integrations/codex_desktop'))
from conversation_deployment import apply
from permission_migration import _append_tool_overrides, _replace_enabled_tools, validate_stdio_server
from local_ai_connector.adapter_socket import SocketAdapter
from local_ai_connector.wakeup import AdapterError

SIDECAR = 'local-ai-connector-native-antigravity'


def prepare(root, data, home):
    server_path = data / 'server.json'
    server = json.loads(server_path.read_bytes())
    endpoint = json.loads((data / 'gemini.json').read_bytes())
    binding = json.loads((data / 'wakeup.json').read_bytes())['bindings']['gemini']
    target = binding['target']
    if (endpoint.get('peer') != 'gemini' or endpoint.get('token') != server['peers']['gemini']
            or endpoint.get('client') != 'generic' or binding.get('adapter') != 'socket'
            or set(target) != {'conversation_id', 'project_id', 'workspace_uri'}):
        raise ValueError('The existing Antigravity endpoint and wake target must match this installation')
    socket = data / 'bridges/antigravity-native.sock'
    registration = {'provider': 'antigravity_sidecar', 'project_id': target['project_id'],
                    'workspace_uri': target['workspace_uri'], 'socket': str(socket)}
    old = server.setdefault('conversations', {}).get('gemini')
    if old is not None and old != registration:
        raise ValueError('A different Antigravity native provider is already registered')
    server['conversations']['gemini'] = registration
    codex_path = home / '.codex/config.toml'
    codex_source = codex_path.read_text()
    codex_server = tomllib.loads(codex_source)['mcp_servers']['local_ai_connector']
    validate_stdio_server(codex_server, command=str(root / '.venv/bin/python'),
        args=['-m', 'local_ai_connector.cli', 'mcp', '--config', str(data / 'gpt.json')],
        env={'PYTHONPATH': str(root / 'src')})
    enabled = codex_server.get('enabled_tools')
    if not isinstance(enabled, list) or any(not isinstance(name, str) for name in enabled):
        raise ValueError('Expected an explicit Codex connector tool list')
    if 'connector_archive' not in enabled:
        codex_source = _replace_enabled_tools(codex_source, [*enabled, 'connector_archive'])
    codex_source, _ = _append_tool_overrides(codex_source, 'local_ai_connector', ('connector_archive',))
    config_path = home / '.gemini/config/config.json'
    config = json.loads(config_path.read_bytes())
    sidecars = config.setdefault('sidecars', {})
    setting = {'enabled': True, 'projectId': target['project_id']}
    if SIDECAR in sidecars and sidecars[SIDECAR] != setting:
        raise ValueError('The native sidecar already has a conflicting setting')
    sidecars[SIDECAR] = setting
    sidecar_path = home / '.gemini/config/sidecars' / SIDECAR / 'sidecar.json'
    sidecar = {'command': str(root / '.venv/bin/python'), 'args': ['-m', 'local_ai_connector.adapter_socket',
        '--socket', str(socket), '--timeout', '45', '--native-conversations', '--',
        str(root / '.venv/bin/python'), str(root / 'integrations/antigravity/native_bridge.py')],
        'env': {'PYTHONPATH': str(root / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'},
        'restart_policy': 'on-failure', 'display_name': 'Connector native Antigravity conversations',
        'description': 'Creates approved native chats and archives them only after a separate requester approval.'}
    if sidecar_path.exists() and json.loads(sidecar_path.read_bytes()) != sidecar:
        raise ValueError('Existing sidecar definition differs; no replacement was made')
    project_path = home / '.gemini/config/projects' / (target['project_id'] + '.json')
    project = json.loads(project_path.read_bytes())
    wrapper = project['permissionGrants']
    if wrapper.get('v2Migrated') is not True:
        raise ValueError('Antigravity project permissions have an unsupported schema')
    grants = wrapper['permissionGrants']
    permission = 'mcp(local_ai_connector/connector_archive)'
    for bucket in ('allow', 'ask', 'deny'):
        if not isinstance(grants.get(bucket, []), list) or any(not isinstance(s, str) for s in grants.get(bucket, [])):
            raise ValueError('Invalid Antigravity project permission entries')
    if any(fnmatch.fnmatchcase(permission, grant) for bucket in ('ask', 'deny') for grant in grants.get(bucket, [])):
        raise ValueError('An explicit Antigravity ask/deny rule conflicts with connector_archive')
    if permission not in grants.get('allow', []):
        grants.setdefault('allow', []).append(permission)
    objects = {server_path: server, sidecar_path: sidecar, project_path: project, config_path: config}
    # Enable the sidecar after its definition and the broker configuration exist.
    replacements = {p: (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode() for p, value in objects.items()}
    replacements[codex_path] = codex_source.encode()
    originals = {}
    for path in replacements:
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != os.getuid())):
            raise ValueError('Expected owner-controlled configuration: ' + str(path))
        if not path.exists() and path != sidecar_path:
            raise ValueError('Required configuration is missing: ' + str(path))
        originals[path] = path.read_bytes() if path.exists() else None
    return originals, replacements, target, socket


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    home = Path.home()
    data = home / '.local/share/local-ai-connector'
    originals, replacements, target, socket = prepare(ROOT, data, home)
    changed = [p for p in originals if originals[p] != replacements[p]]
    for path in changed:
        print('Update:', path)
    if not changed:
        print('Already installed. Reload the connector tools in requesting apps before acceptance.')
    elif not args.apply:
        print('Dry run only. This enables new Antigravity chats and separately approved archives. No deletion capability.')
    else:
        def readiness():
            async def check():
                adapter = SocketAdapter(str(socket), 5)
                last = None
                for _ in range(15):
                    try:
                        if await adapter.confirm(target) != target:
                            raise RuntimeError('Native sidecar reported the wrong project/workspace')
                        return
                    except AdapterError as exc:
                        last = exc
                        await asyncio.sleep(1)
                raise RuntimeError('Native sidecar did not become ready: ' + str(last))
            asyncio.run(check())
        apply(ROOT, data, originals, replacements, home / 'Library/LaunchAgents/dev.local-ai-connector.service.plist',
              readiness=readiness)
        print('Native sidecar confirmed the existing project/workspace. Reopen Codex and reload requester MCP tools.')
        print('Creation and archive task approvals remain separate. Initiate acceptance manually after reloading.')


if __name__ == '__main__':
    main()
