#!/bin/zsh
# Run in macOS Terminal to register one dedicated Codex Desktop chat as the ingress worker.
set -eu
connector_root="${0:A:h:h}"
exec "$connector_root/.venv/bin/python" - "$connector_root" "$@" <<'PY'
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from uuid import UUID

root = Path(sys.argv[1])
parser = argparse.ArgumentParser(prog='register-codex-desktop.command')
parser.add_argument('--thread-id', required=True, help='ID of the existing dedicated Codex Desktop chat')
parser.add_argument('--workspace', required=True, type=Path, help='absolute workspace directory of that chat')
options = parser.parse_args(sys.argv[2:])
try:
    valid_thread = str(UUID(options.thread_id)) == options.thread_id
except ValueError:
    valid_thread = False
if not valid_thread or not options.workspace.is_absolute():
    raise SystemExit('Pass the exact Codex thread ID and an absolute workspace path. Nothing changed.')
sys.path.insert(0, str(root / 'src'))
from local_ai_connector.registry import add_peer
from local_ai_connector.wakeup import CommandAdapter, load_config
sys.path.insert(0, str(root / 'integrations/codex_desktop'))
from deployment_config import project_mcp_config, wake_binding

data = Path.home() / '.local/share/local-ai-connector'
peer = 'codex_desktop'
thread_id = options.thread_id
workspace = options.workspace
target = {'thread_id': thread_id, 'workspace': str(workspace)}
identity = {'namespace': 'codex', 'path': ['x-codex-turn-metadata', 'thread_id']}
bound_session = 'codex:' + thread_id
profile = {
    'name': 'Codex Desktop inbound worker',
    'description': 'Dedicated Codex Desktop worker for approved connector requests. Use American English; the model is selected in the app.',
    'capabilities': ['question-answering', 'text-generation'],
}
endpoint_path = data / f'{peer}.json'
server_path, wake_path = data / 'server.json', data / 'wakeup.json'
project_config = workspace / '.codex' / 'config.toml'
label = 'dev.local-ai-connector.service'
domain = f'gui/{os.getuid()}'
job = f'{domain}/{label}'
plist_path = Path.home() / 'Library/LaunchAgents' / f'{label}.plist'
expected = [str(root / '.venv/bin/python'), '-m', 'local_ai_connector.cli', '--data', str(data), 'serve']
plist = plistlib.loads(plist_path.read_bytes())
if (plist.get('Label') != label or plist.get('ProgramArguments') != expected
        or plist.get('EnvironmentVariables', {}).get('PYTHONPATH') != str(root / 'src')
        or plist.get('KeepAlive') is not True or plist.get('RunAtLoad') is not True):
    raise SystemExit('The installed service definition differs from this project. Nothing changed.')

server_original = server_path.read_bytes()
wake_original = wake_path.read_bytes()
server = json.loads(server_original)
wake = json.loads(wake_original)
if wake.get('enabled') is not True:
    raise SystemExit('The existing wake-up dispatcher is disabled. Enable it through its reviewed owner configuration before registering this worker.')
if peer in server.get('peers', {}) or peer in wake.get('bindings', {}) or endpoint_path.exists():
    raise SystemExit('A codex_desktop endpoint or wake binding already exists. Nothing changed.')
if project_config.exists():
    raise SystemExit('The dedicated project already has .codex/config.toml. Nothing changed.')


def no_open_channels():
    with sqlite3.connect((data / 'state.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        count = db.execute("SELECT count(*) FROM channels WHERE status IN ('pending','active')").fetchone()[0]
    if count:
        raise RuntimeError('A connector task is pending or active. Finish it before maintenance.')


def check_target_idle():
    bridge = root / 'integrations/codex_desktop/bridge.py'
    request = json.dumps({'op': 'status', 'target': target})
    result = subprocess.run(
        [str(root / '.venv/bin/python'), str(bridge)],
        input=request, capture_output=True, text=True, timeout=10, check=True,
    )
    status = json.loads(result.stdout)
    if status != {'ok': True, 'state': 'idle'}:
        raise RuntimeError('The pinned Codex Desktop thread is not the expected idle workspace.')


def process_state():
    listing = subprocess.check_output(['/bin/launchctl', 'list'], text=True)
    jobs = [line.split() for line in listing.splitlines()
            if len(line.split()) == 3 and line.split()[2] == label]
    if len(jobs) > 1:
        raise RuntimeError('Multiple matching launchd jobs were reported')
    pid = jobs[0][0] if jobs and jobs[0][0].isdigit() else None
    result = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{server["port"]}', '-sTCP:LISTEN', '-t'],
                            capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError('Unable to inspect the service listener: ' + result.stderr.strip())
    listeners = set(result.stdout.split())
    if listeners and (not pid or listeners != {pid}):
        raise RuntimeError('The listener is not owned by the installed launchd service')
    if pid:
        uid = subprocess.check_output(['/bin/ps', '-p', pid, '-o', 'uid='], text=True).strip()
        command = subprocess.check_output(['/bin/ps', '-ww', '-p', pid, '-o', 'command='], text=True).strip()
        if uid != str(os.getuid()) or command != ' '.join(expected):
            raise RuntimeError('The listening process identity differs from the installed definition')
    return bool(jobs), pid if listeners else None


def restore_service():
    try:
        loaded, _ = process_state()
        if not loaded:
            subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist_path)], check=True)
        for _ in range(40):
            _, pid = process_state()
            if pid:
                print(f'Shared service verified: launchd PID {pid} owns port {server["port"]}.', flush=True)
                return True
            time.sleep(1)
        raise RuntimeError('The service did not become ready within 40 seconds')
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Service recovery is incomplete: {exc}', file=sys.stderr)
        print('Recovery: ' + shlex.join(['/bin/launchctl', 'bootstrap', domain, str(plist_path)]), file=sys.stderr)
        print('If already loaded: ' + shlex.join(['/bin/launchctl', 'kickstart', job]), file=sys.stderr)
        print('Inspect launchctl print for the service and verify its listener before starting new tasks. Do not rerun this installer.', file=sys.stderr)
        return False


def atomic_private_write(path: Path, content: bytes):
    fd, temporary = tempfile.mkstemp(prefix=path.name + '-', suffix='.tmp', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
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


def create_private_file(path: Path, content: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
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


check_target_idle()
no_open_channels()
loaded, _ = process_state()
if not loaded:
    raise SystemExit('The expected connector launchd service is not loaded. Nothing changed.')
if process_state()[1] is None:
    raise SystemExit('The expected launchd service does not own its listener. Nothing changed.')

binding = wake_binding(root, target)
config_text = project_mcp_config(root, endpoint_path)
registered = False
wake_changed = False
codex_dir_created = False
project_config_created = False
try:
    subprocess.run(['/bin/launchctl', 'bootout', job], check=True)
    with (data / 'server.lock').open('a') as lock:
        for _ in range(50):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        else:
            raise RuntimeError('The previous service has not stopped')
        no_open_channels()
        if server_path.read_bytes() != server_original or wake_path.read_bytes() != wake_original:
            raise RuntimeError('Connector configuration changed during maintenance; registration canceled')
        check_target_idle()
    add_peer(data, peer, profile, identity, mcp_locale='en-US')
    registered = True
    endpoint = json.loads(endpoint_path.read_bytes())
    endpoint['bound_session'] = bound_session
    atomic_private_write(endpoint_path, (json.dumps(endpoint, indent=2) + '\n').encode())
    if wake_path.read_bytes() != wake_original:
        raise RuntimeError('Wake-up config changed during maintenance; registration canceled')
    wake['bindings'][peer] = binding
    fd, temporary = tempfile.mkstemp(prefix='wakeup-', suffix='.tmp', dir=data)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(wake, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        wake_changed = True
        os.replace(temporary, wake_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    loaded_bindings, options = load_config(data, json.loads(server_path.read_text())['peers'])
    if (options is None or peer not in loaded_bindings
            or not isinstance(loaded_bindings[peer].adapter, CommandAdapter)
            or loaded_bindings[peer].target != target):
        raise RuntimeError('The registered Codex Desktop wake-up binding failed validation')
    if not project_config.parent.exists():
        project_config.parent.mkdir(mode=0o700)
        codex_dir_created = True
    elif project_config.parent.is_symlink() or not project_config.parent.is_dir():
        raise RuntimeError('The project .codex path is not a normal directory')
    if project_config.exists():
        raise RuntimeError('The project config appeared during deployment; existing file was preserved')
    create_private_file(project_config, config_text.encode())
    project_config_created = True
except BaseException:
    if project_config_created:
        project_config.unlink()
    if codex_dir_created:
        project_config.parent.rmdir()
    if registered:
        if wake_changed:
            wake_path.write_bytes(wake_original)
        server_path.write_bytes(server_original)
        endpoint_path.unlink(missing_ok=True)
    raise
finally:
    restored = restore_service()

if not restored:
    raise SystemExit(1)
print('Codex Desktop worker registered and bound to the dedicated ingress thread.')
print(f'Project MCP configuration: {project_config}')
print('Reload MCP servers for this Codex project before testing. Global requester, Gemini, Claude, and hook configuration were not edited.')
PY
