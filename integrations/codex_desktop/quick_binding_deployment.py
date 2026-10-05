"""Prepare the two existing initiating MCP launchers without changing their approval policies."""
import json
import os
from pathlib import Path
import stat

from integrations.codex_desktop.conversation_deployment import prepare as ingress_configuration
from integrations.codex_desktop.permission_migration import migrate_antigravity


def prepare(root, data, claude_config, antigravity_config, codex_cli, candidate_python, previous_claude_python,
            *, antigravity_project=None):
    guards = {}
    for path in (data / 'wakeup.json', data / 'codex_desktop.json'):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Expected an owner-controlled regular configuration: ' + str(path))
        guards[path] = path.read_bytes()
    originals, ingress_replacements = ingress_configuration(root, data, 'codex_desktop')
    if any(originals[path] != content for path, content in ingress_replacements.items()):
        raise ValueError('Install and trust the existing Codex ingress through its normal setup first; quick binding does not install hooks.')
    for executable in (codex_cli, candidate_python, previous_claude_python):
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError('Expected an installed executable: ' + str(executable))
    server_path = data / 'server.json'
    server = json.loads(originals[server_path])
    if server.get('codex_binding_cli') not in (None, str(codex_cli)):
        raise ValueError('Another Codex metadata source is configured; review it before replacement.')
    replacements = {}
    for peer, host_config, transport, command, prefix in (
        ('claude_code', claude_config, 'host_tool_permission', candidate_python, ['-E', '-B']),
        ('gemini', antigravity_config, 'elicitation', root / '.venv/bin/python', []),
    ):
        endpoint_path = data / (peer + '.json')
        for path in (endpoint_path, host_config):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('Expected an owner-controlled regular configuration: ' + str(path))
            originals[path] = path.read_bytes()
        endpoint = json.loads(originals[endpoint_path])
        if (endpoint.get('peer') != peer or endpoint.get('client') != 'generic'
                or endpoint.get('tool_profile') != 'participant' or endpoint.get('mcp_locale') != 'en-US'
                or endpoint.get('approval_transport', 'elicitation') != transport
                or endpoint.get('token') != server['peers'].get(peer)
                or not endpoint.get('approval_token')
                or endpoint['approval_token'] != server.get('approval_tokens', {}).get(peer)):
            raise ValueError('Existing initiating endpoint identity or approval configuration changed: ' + peer)
        document = json.loads(originals[host_config])
        entry = document.get('mcpServers', {}).get('local_ai_connector')
        args = [*prefix, '-m', 'local_ai_connector.cli', 'mcp', '--config', str(endpoint_path)]
        if peer == 'claude_code':
            args += ['--claude-binding-root', str(root)]
        if (not isinstance(entry, dict) or entry.get('command') not in
                (str(command), str(previous_claude_python) if peer == 'claude_code' else str(command))
                or entry.get('args') not in (args, [*args, '--codex-quick-binding'])
                or entry.get('disabled') is True):
            raise ValueError('The existing MCP launcher differs from the expected local endpoint: ' + peer)
        tools = {'connector_codex_binding_catalog', 'connector_codex_bind_chat',
                 'connector_codex_bound_chat', 'connector_codex_quick_bind'}
        if tools.intersection(entry.get('disabledTools', [])):
            raise ValueError('An explicit host tool restriction conflicts with quick binding: ' + peer)
        if peer == 'gemini' and entry.get('env', {}).get('PYTHONPATH') != str(root / 'src'):
            raise ValueError('Antigravity source runtime points elsewhere.')
        entry['command'] = str(command)
        entry['args'] = [*args, '--codex-quick-binding']
        replacements[host_config] = (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode()
    server['codex_binding_cli'] = str(codex_cli)
    replacements[server_path] = (json.dumps(server, ensure_ascii=False, indent=2) + '\n').encode()
    if antigravity_project is not None:
        info = antigravity_project.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Expected an owner-controlled Antigravity project configuration.')
        originals[antigravity_project] = antigravity_project.read_bytes()
        document = json.loads(originals[antigravity_project])
        wake = json.loads(guards[data / 'wakeup.json'])
        if document.get('id') != wake['bindings']['gemini']['target']['project_id']:
            raise ValueError('Permission scope must be the currently registered Antigravity project.')
        updated, _ = migrate_antigravity(document, tool_names=(
            'connector_codex_binding_catalog', 'connector_codex_bound_chat',
            'connector_codex_bind_chat', 'connector_codex_quick_bind'))
        replacements[antigravity_project] = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode()
    if any(path.read_bytes() != content for path, content in guards.items()):
        raise ValueError('The existing ingress or project binding changed during preparation.')
    originals.update(guards)
    return originals, replacements
