#!/bin/zsh
# Preview the narrow MCP policy migration; the owner must pass --apply to write it.
set -eu
connector_root="${0:A:h:h}"
exec "$connector_root/.venv/bin/python" - "$connector_root" "$@" <<'PY'
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from datetime import datetime, timezone
import tomllib
from uuid import UUID

root = Path(sys.argv[1])
parser = argparse.ArgumentParser(prog='migrate-codex-communication-permissions.command')
parser.add_argument('--worker-project', required=True, type=Path,
                    help='absolute workspace of the dedicated Codex Desktop worker')
parser.add_argument('--antigravity-project-id', required=True, type=UUID,
                    help='ID of the Antigravity project whose MCP permissions are migrated')
parser.add_argument('--apply', action='store_true', help='write the change; the default is a preview')
options = parser.parse_args(sys.argv[2:])
if not options.worker_project.is_absolute():
    raise SystemExit('--worker-project must be an absolute path. Nothing changed.')
apply = options.apply
sys.path.insert(0, str(root / 'integrations/codex_desktop'))
from permission_migration import (
    migrate_antigravity, migrate_codex_global, migrate_codex_worker,
    validate_stdio_server,
)

data = Path.home() / '.local/share/local-ai-connector'
python = str(root / '.venv/bin/python')
source_env = {'PYTHONPATH': str(root / 'src')}
global_runtime = Path.home() / '.codex/config.toml'
worker_project = options.worker_project
gemini_runtime = Path.home() / '.gemini/config/mcp_config.json'
runtime_originals = {
    global_runtime: global_runtime.read_bytes(),
    worker_project / '.codex/config.toml': (worker_project / '.codex/config.toml').read_bytes(),
    gemini_runtime: gemini_runtime.read_bytes(),
}
global_server = tomllib.loads(runtime_originals[global_runtime].decode())['mcp_servers']['local_ai_connector']
worker_server = tomllib.loads(runtime_originals[worker_project / '.codex/config.toml'].decode())['mcp_servers']['local_ai_connector_codex_desktop_worker']
gemini_server = json.loads(runtime_originals[gemini_runtime]).get('mcpServers', {}).get('local_ai_connector')
base_args = ['-m', 'local_ai_connector.cli', 'mcp', '--config']
validate_stdio_server(global_server, command=python,
                      args=[*base_args, str(data / 'gpt.json')], env=source_env)
validate_stdio_server(worker_server, command=python,
                      args=[*base_args, str(data / 'codex_desktop.json')], env=source_env)
validate_stdio_server(gemini_server, command=python,
                      args=[*base_args, str(data / 'gemini.json')],
                      env={**source_env, 'PYTHONDONTWRITEBYTECODE': '1'})

paths = {
    global_runtime: migrate_codex_global,
    worker_project / '.codex/config.toml': migrate_codex_worker,
    Path.home() / f'.gemini/config/projects/{options.antigravity_project_id}.json': None,
}
originals = {}
replacements = {}
changes = {}
file_modes = {}
for path, transform in paths.items():
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise SystemExit(f'Refusing unsafe or non-user-owned config: {path}')
    original = path.read_bytes()
    originals[path] = original
    file_modes[path] = stat.S_IMODE(info.st_mode)
    if transform is None:
        updated, changed = migrate_antigravity(json.loads(original))
        replacement = ((json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode()
                       if changed else original)
    else:
        updated, changed = transform(original.decode('utf-8'))
        replacement = updated.encode('utf-8')
    changes[path] = changed
    replacements[path] = replacement

modified = [path for path in paths if replacements[path] != originals[path]]
if not modified:
    print('All requested permission settings are already present; nothing to change.')
    raise SystemExit(0)

for path in modified:
    print(f'{path}:')
    for change in changes[path]:
        print(f'  + {change}')
if not apply:
    print('Preview only. Run this command with --apply from the owner Terminal to apply and back up these changes.')
    raise SystemExit(0)

for path, original in originals.items():
    if path.read_bytes() != original:
        raise SystemExit(f'Config changed after preview; nothing written: {path}')
for path, original in runtime_originals.items():
    if path not in originals and path.read_bytes() != original:
        raise SystemExit(f'MCP server runtime config changed after preview; nothing written: {path}')

stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backups = {path: path.with_name(path.name + '.backup-' + stamp) for path in modified}
created_backups = []
written = []


def write_backup(path: Path, content: bytes, mode: int):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb', closefd=False) as file:
            file.write(content)
            file.flush()
            os.fsync(fd)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


def atomic_replace(path: Path, content: bytes, mode: int):
    fd, temporary = tempfile.mkstemp(prefix=path.name + '-', suffix='.tmp', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


try:
    for path in modified:
        write_backup(backups[path], originals[path], file_modes[path])
        created_backups.append(backups[path])
    for path in modified:
        if path.read_bytes() != originals[path]:
            raise RuntimeError(f'Config changed immediately before replacement: {path}')
        for guard_path, original in runtime_originals.items():
            if guard_path not in originals and guard_path.read_bytes() != original:
                raise RuntimeError(f'MCP server runtime config changed before replacement: {guard_path}')
        written.append(path)
        atomic_replace(path, replacements[path], file_modes[path])
except BaseException:
    for path in reversed(written):
        if path.read_bytes() == replacements[path]:
            atomic_replace(path, originals[path], file_modes[path])
        else:
            print(f'Concurrent config change preserved; restore manually from {backups[path]}', file=sys.stderr)
    for backup in created_backups:
        print(f'Backup retained: {backup}', file=sys.stderr)
    raise

for path in modified:
    print(f'Applied; backup: {backups[path]}')
print('Reload the Codex MCP clients and Antigravity MCP client before human-led acceptance. No connector service restart is needed.')
PY
