"""Inspect and explicitly bind a pre-created Claude Desktop Code worker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'integrations/codex_desktop'))
from local_ai_connector.claude_binding import confirmed_session, socket_identity, inspect_identity, compatibility_issues
from integrations.codex_desktop.conversation_deployment import apply, listener, replace
from local_ai_connector.conversations import canonical
from local_ai_connector.wakeup import load_config, parse_config


def binding_digest(binding):
    return hashlib.sha256(canonical(binding).encode()).hexdigest()


def inspect_target(metadata, desktop_id, cli_id, workspace, record):
    identity = inspect_identity(metadata, desktop_id, cli_id, workspace)
    target = {'session_id': cli_id, 'workspace': str(workspace)}
    confirmed_session(target, record)
    return identity, target


def capture(metadata, desktop_id, cli_id, workspace, record, socket_path, *, write=False,
            expected_socket_identity=None, rollback_receipt=None):
    identity = inspect_identity(metadata, desktop_id, cli_id, workspace)
    if (not record.is_absolute() or record.name != cli_id + '.json'
            or not socket_path.is_absolute() or not record.parent.is_dir()):
        raise ValueError('Use absolute inbox/record paths and an existing record directory')
    previous = None
    if record.exists() or record.is_symlink():
        with os.fdopen(os.open(record, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('Existing session record must be an owner-only regular file')
            previous = file.read()
        old = json.loads(previous)
        if old.get('session_id') != cli_id or old.get('workspace') != str(workspace):
            raise ValueError('Recapture cannot replace a different session/workspace')
    info = socket_identity(socket_path)
    if expected_socket_identity is not None and (info.st_dev, info.st_ino) != expected_socket_identity:
        raise ValueError('Chat inbox changed after preview; obtain a fresh selected-chat receipt')
    proposed = {'session_id': cli_id, 'workspace': str(workspace), 'socket': str(socket_path),
        'socket_dev': info.st_dev, 'socket_ino': info.st_ino, 'desktop_id': desktop_id,
        'source': 'explicit socket observed inside selected Claude chat'}
    if write:
        inspect_identity(metadata, desktop_id, cli_id, workspace)
        current = socket_identity(socket_path)
        if ((record.read_bytes() if record.exists() else None) != previous
                or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError('Session record or inbox changed while preparing capture')
        content = (json.dumps(proposed, indent=2) + '\n').encode()
        if rollback_receipt is not None:
            rollback_receipt.update(previous=previous, written=content)
        try:
            replace(record, content)
            confirmed_session({'session_id': cli_id, 'workspace': str(workspace)}, record)
        except BaseException as failure:
            observed = record.read_bytes() if record.exists() else None
            if observed == content:
                if previous is None:
                    record.unlink()
                else:
                    replace(record, previous)
            elif observed != previous:
                raise BaseExceptionGroup('Capture failed and a concurrent record was retained',
                    [failure, RuntimeError('Session record changed during capture recovery')])
            raise
    return {'session': identity, 'record': str(record), 'mode': 'captured' if write else 'capture_preview',
        'socket': str(socket_path), 'socket_dev': info.st_dev, 'socket_ino': info.st_ino,
        'provenance': 'Caller must retain the actual selected-chat address receipt; a path alone is not authentication.'}


def quiet(data):
    with sqlite3.connect((data / 'state.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        if db.execute("SELECT count(*) FROM channels WHERE status IN ('pending','active')").fetchone()[0]:
            raise ValueError('Finish all pending/active connector channels before changing the worker binding')


def prepare(root, data, metadata, desktop_id, cli_id, workspace, record, *, replace_binding=None, create_binding=False):
    identity, target = inspect_target(metadata, desktop_id, cli_id, workspace, record)
    return prepare_configuration(root, data, identity, target, record,
        replace_binding=replace_binding, create_binding=create_binding)


def prepare_configuration(root, data, identity, target, record, *, replace_binding=None, create_binding=False):
    """Build the same candidate after either a persisted or just-previewed capture."""
    paths = [data / 'server.json', data / 'wakeup.json', data / 'claude_code.json']
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
            raise ValueError('Expected existing owner-controlled connector configuration: ' + str(path))
    originals = {path: path.read_bytes() for path in paths}
    server, wake, endpoint = (json.loads(originals[path]) for path in paths)
    if (endpoint.get('peer') != 'claude_code' or endpoint.get('client') != 'generic'
            or endpoint.get('url') != server['url']
            or endpoint.get('token') != server['peers'].get('claude_code')
            or not isinstance(endpoint.get('token'), str) or not endpoint['token']):
        raise ValueError('Existing Claude endpoint does not match the shared service; no credential was changed')
    if endpoint.get('tool_profile') == 'participant':
        if (not endpoint.get('approval_token')
                or endpoint['approval_token'] != server.get('approval_tokens', {}).get('claude_code')):
            raise ValueError('Existing Claude approval endpoint does not match')
    old = wake['bindings'].get('claude_code')
    expected_command = [str(root / '.venv/bin/python'), str(root / 'integrations/claude_code/bridge.py')]
    if old is None:
        if not create_binding or replace_binding is not None:
            raise ValueError('The first Claude binding requires explicit create_binding confirmation')
        digest = None
        new = {'adapter': 'command', 'timeout': 10, 'restore': False}
    elif not isinstance(old, dict):
        raise ValueError('Invalid existing Claude binding')
    elif (old.get('adapter') != 'command' or old.get('command', [])[:2] != expected_command
            or len(old.get('command', [])) != 4 or old['command'][2] != '--session-record'):
        raise ValueError('The existing Claude adapter differs from this installation')
    else:
        digest = binding_digest(old)
        new = dict(old)
    new.update(target=target, command=[*expected_command, '--session-record', str(record)])
    changed = old != new
    if replace_binding is not None and replace_binding != digest:
        raise ValueError('The expected previous binding changed')
    replacements = {}
    if changed:
        wake['bindings']['claude_code'] = new
        replacements[paths[1]] = (json.dumps(wake, indent=2, ensure_ascii=False) + '\n').encode()
    bindings, options = parse_config(wake, server['peers'])
    issues = compatibility_issues(bindings, options)
    return originals, replacements, {'session': identity, 'target': target,
        'previous_binding_sha256': digest, 'binding_changes': changed,
        'record': str(record), 'inbox_identity_snapshot_matches': True,
        'replacement_requires_confirmation': changed and old is not None and replace_binding is None,
        'first_binding': old is None,
        'wake_compatibility_issues': issues,
        'runtime_idle': 'unknown', 'mcp_available': 'requires_live_test'}


def install(root, data, originals, replacements, metadata, desktop_id, cli_id, workspace, record, plist_path,
            *, replace_binding=None, create_binding=False, extra_readiness=None, extra_validation=None):
    def validate():
        quiet(data)
        inspect_target(metadata, desktop_id, cli_id, workspace, record)
        if extra_validation is not None:
            extra_validation()

    server = json.loads(originals[data / 'server.json'])
    candidate = json.loads(replacements.get(data / 'wakeup.json', originals[data / 'wakeup.json']))
    bindings, options = parse_config(candidate, server['peers'])
    issues = compatibility_issues(bindings, options)
    if issues:
        raise ValueError('; '.join(issues))

    def ready():
        inspect_target(metadata, desktop_id, cli_id, workspace, record)
        bindings, options = load_config(data, json.loads((data / 'server.json').read_bytes())['peers'])
        issues = compatibility_issues(bindings, options)
        if issues:
            raise ValueError('; '.join(issues))
        binding = bindings['claude_code']
        if (binding.target != {'session_id': cli_id, 'workspace': str(workspace)}
                or binding.adapter.command != candidate['bindings']['claude_code']['command']):
            raise RuntimeError('Installed Claude binding differs; do not send a task')
        if extra_readiness is not None:
            extra_readiness()

    if replacements:
        old = json.loads(originals[data / 'wakeup.json'])['bindings'].get('claude_code')
        if old is None and not create_binding:
            raise ValueError('The first Claude binding requires explicit create_binding confirmation')
        if old is not None and replace_binding != binding_digest(old):
            raise ValueError('Replacement requires the exact inspected previous binding digest')
        validate()
        apply(root, data, originals, replacements, plist_path, precommit=validate, readiness=ready)
    else:
        inspect_target(metadata, desktop_id, cli_id, workspace, record)
        if any(path.read_bytes() != content for path, content in originals.items()):
            raise ValueError('Configuration changed during inspection')
        expected = [str(root / '.venv/bin/python'), '-m', 'local_ai_connector.cli', '--data', str(data), 'serve']
        server = json.loads(originals[data / 'server.json'])
        if listener(server['port'], expected) is None:
            raise RuntimeError('The configured connector service is not running')
        ready()


def main():
    if not sys.argv[1:]:
        from integrations.claude_code.guided_binding import run
        try:
            return run()
        except (ValueError, OSError) as exc:
            raise SystemExit('绑定未完成：' + str(exc)) from None
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--desktop-id', required=True)
    parser.add_argument('--cli-id', required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--session-record', type=Path, required=True)
    parser.add_argument('--data', type=Path, default=Path.home() / '.local/share/local-ai-connector')
    parser.add_argument('--replace-binding', help='SHA-256 of the exact previous binding; never inferred from a chat title')
    parser.add_argument('--create-binding', action='store_true', help='Explicitly create the first binding for an already registered Claude endpoint')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--capture-socket', type=Path, help='Capture ONLY an inbox address actually displayed by this chat; no binding/service change')
    args = parser.parse_args()
    if args.capture_socket is not None:
        if args.replace_binding is not None:
            parser.error('Capture and binding replacement are separate operations')
        print(json.dumps(capture(args.metadata, args.desktop_id, args.cli_id, args.workspace,
            args.session_record, args.capture_socket, write=args.apply), ensure_ascii=False, indent=2))
        return
    originals, replacements, report = prepare(ROOT, args.data, args.metadata, args.desktop_id,
        args.cli_id, args.workspace, args.session_record, replace_binding=args.replace_binding,
        create_binding=args.create_binding)
    if args.apply:
        install(ROOT, args.data, originals, replacements, args.metadata, args.desktop_id,
            args.cli_id, args.workspace, args.session_record,
            Path.home() / 'Library/LaunchAgents/dev.local-ai-connector.service.plist', replace_binding=args.replace_binding,
            create_binding=args.create_binding)
    expected = [str(ROOT / '.venv/bin/python'), '-m', 'local_ai_connector.cli', '--data', str(args.data), 'serve']
    report['service_running'] = listener(json.loads(originals[args.data / 'server.json'])['port'], expected) is not None
    report['mode'] = 'applied' if args.apply else 'read_only'
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
