#!/bin/zsh
# Enable cold-session restore for the already registered Codex Desktop ingress worker.
set -eu
connector_root="${0:A:h:h}"
exec "$connector_root/.venv/bin/python" - "$connector_root" "$@" <<'PY'
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time

root = Path(sys.argv[1]).resolve()
parser = argparse.ArgumentParser(prog='enable-codex-desktop-cold-restore.command')
parser.add_argument('--thread-id', required=True, help='Codex thread ID of the registered codex_desktop worker')
parser.add_argument('--workspace', required=True, help='absolute workspace of the registered worker')
parser.add_argument('--apply', action='store_true', help='write the change; the default is a dry run')
options = parser.parse_args(sys.argv[2:])
apply = options.apply
sys.path.insert(0, str(root / 'src'))
from local_ai_connector.wakeup import CommandAdapter, load_config

data = Path.home() / '.local/share/local-ai-connector'
peer = 'codex_desktop'
# The registered binding must match this exact owner-supplied target before anything changes.
thread_id = options.thread_id
workspace = options.workspace
target = {'thread_id': thread_id, 'workspace': workspace}
bridge = root / 'integrations/codex_desktop/bridge.py'
python = root / '.venv/bin/python'
label = 'dev.local-ai-connector.service'
domain = f'gui/{os.getuid()}'
job = f'{domain}/{label}'
plist_path = Path.home() / 'Library/LaunchAgents' / f'{label}.plist'
server_path, wake_path = data / 'server.json', data / 'wakeup.json'
expected_command = [str(python), str(bridge)]
expected_service = [str(python), '-m', 'local_ai_connector.cli', '--data', str(data), 'serve']

for path in (server_path, wake_path):
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f'{path} is not a regular configuration file. Nothing changed.')
server_original, wake_original = server_path.read_bytes(), wake_path.read_bytes()
server, wake = json.loads(server_original), json.loads(wake_original)
if wake.get('enabled') is not True:
    raise SystemExit('The existing wake-up dispatcher is disabled. Nothing changed.')
peers = server.get('peers')
bindings = wake.get('bindings')
if not isinstance(peers, dict) or peer not in peers or not isinstance(bindings, dict) or peer not in bindings:
    raise SystemExit('The registered codex_desktop endpoint or wake binding is missing. Nothing changed.')
endpoint_path = data / f'{peer}.json'
if endpoint_path.is_symlink() or not endpoint_path.is_file():
    raise SystemExit('The codex_desktop endpoint file is not a regular file. Nothing changed.')
endpoint = json.loads(endpoint_path.read_text())
if endpoint.get('bound_session') != 'codex:' + thread_id:
    raise SystemExit('The registered endpoint is bound to a different Codex thread. Nothing changed.')

binding = bindings[peer]
if not isinstance(binding, dict) or binding.get('adapter') != 'command':
    raise SystemExit('The codex_desktop binding is not a command adapter. Nothing changed.')
if binding.get('target') != target or binding.get('command') != expected_command:
    raise SystemExit('The codex_desktop binding target or bridge command differs from the registered worker. Nothing changed.')
if binding.get('restore', False) is not False:
    if binding.get('restore') is True:
        raise SystemExit('Cold-session restore is already enabled; no changes are needed.')
    raise SystemExit('The existing restore option is not a boolean false. Nothing changed.')

plist = plistlib.loads(plist_path.read_bytes())
if (plist.get('Label') != label or plist.get('ProgramArguments') != expected_service
        or plist.get('EnvironmentVariables', {}).get('PYTHONPATH') != str(root / 'src')
        or plist.get('KeepAlive') is not True or plist.get('RunAtLoad') is not True):
    raise SystemExit('The installed connector service definition differs from this project. Nothing changed.')


def channel_gate():
    """Block all pending approvals and unanswered work, not stale active rows alone."""
    db_path = data / 'state.sqlite3'
    with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
        pending = db.execute("SELECT count(*) FROM channels WHERE status='pending'").fetchone()[0]
        unanswered = db.execute(
            "SELECT count(*) FROM messages m JOIN channels c ON c.id=m.channel "
            "WHERE c.status='active' AND m.kind='question' AND m.resolved=0"
        ).fetchone()[0]
    if pending or unanswered:
        raise RuntimeError(f'Maintenance paused: {pending} pending approval(s), {unanswered} unanswered question(s) across the connector.')


def check_target_safe():
    request = json.dumps({'op': 'status', 'target': target})
    result = subprocess.run([str(python), str(bridge)], input=request, capture_output=True,
                            text=True, timeout=20, check=True)
    state = json.loads(result.stdout)
    if state.get('ok') is True:
        if state.get('state') != 'idle':
            raise RuntimeError('The exact registered Codex Desktop thread is not idle.')
        return 'idle'
    if state.get('error') in ('socket_missing', 'no_owner'):
        return 'cold'
    raise RuntimeError('The exact registered Codex Desktop thread state could not be verified safely.')


def process_state():
    listing = subprocess.check_output(['/bin/launchctl', 'list'], text=True)
    jobs = [line.split() for line in listing.splitlines()
            if len(line.split()) == 3 and line.split()[2] == label]
    if len(jobs) > 1:
        raise RuntimeError('Multiple matching connector launchd jobs were reported.')
    if not jobs:
        if subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{server["port"]}', '-sTCP:LISTEN', '-t'],
                          capture_output=True, text=True).stdout.split():
            raise RuntimeError('The connector listener exists without the expected launchd job.')
        return None
    pid = jobs[0][0] if jobs[0][0].isdigit() else None
    result = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{server["port"]}', '-sTCP:LISTEN', '-t'],
                            capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError('Unable to inspect the connector listener: ' + result.stderr.strip())
    listeners = set(result.stdout.split())
    if pid is None and not listeners:
        return None
    if pid is None or listeners != {pid}:
        raise RuntimeError('The connector listener is not owned by the expected launchd process.')
    uid = subprocess.check_output(['/bin/ps', '-p', pid, '-o', 'uid='], text=True).strip()
    command = subprocess.check_output(['/bin/ps', '-ww', '-p', pid, '-o', 'command='], text=True).strip()
    if uid != str(os.getuid()) or command != ' '.join(expected_service):
        raise RuntimeError('The connector process identity differs from the installed service definition.')
    return pid


def atomic_private_write(path, content):
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


def verify_loaded_config(restore_enabled):
    loaded, options = load_config(data, peers)
    selected = loaded.get(peer)
    if (options is None or selected is None or not isinstance(selected.adapter, CommandAdapter)
            or selected.target != target
            or getattr(selected, 'restore_enabled', False) is not restore_enabled):
        raise RuntimeError('The updated codex_desktop binding failed loader validation.')


def wait_ready(previous_pid, restore_enabled):
    last_error = 'launchd has not started the replacement process yet'
    for _ in range(40):
        try:
            pid = process_state()
            if pid is None:
                raise RuntimeError('The expected launchd process has no listener yet.')
            if pid == previous_pid:
                raise RuntimeError('The previous connector process still owns the listener.')
            verify_loaded_config(restore_enabled)
            return pid
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            last_error = str(exc)
            time.sleep(1)
    raise RuntimeError(f'The connector did not return ready within 40 seconds: {last_error}')


channel_gate()
target_state = check_target_safe()
pid = process_state()
if pid is None:
    raise SystemExit('The expected connector launchd service is not ready. Nothing changed.')
print(f'Validated codex_desktop → {thread_id} at {workspace} ({target_state}); launchd PID {pid} owns the connector listener.')
if not apply:
    print('Dry run only. No files or services were changed. Use --apply to enable restore and restart the connector.')
    raise SystemExit(0)

backup = data / f'wakeup.json.backup-codex-cold-restore-{int(time.time())}'
fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'wb') as file:
    file.write(wake_original)
    file.flush()
    os.fsync(file.fileno())
updated = json.loads(wake_original)
updated['bindings'][peer]['restore'] = True
new_bytes = (json.dumps(updated, indent=2) + '\n').encode()
service_touched = False
try:
    channel_gate()
    check_target_safe()
    subprocess.run(['/bin/launchctl', 'bootout', job], check=True)
    service_touched = True
    with (data / 'server.lock').open('a') as lock:
        for _ in range(50):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        else:
            raise RuntimeError('The previous connector process has not released its lock.')
        if server_path.read_bytes() != server_original or wake_path.read_bytes() != wake_original:
            raise RuntimeError('Connector configuration changed during preflight; nothing was applied.')
        channel_gate()
        check_target_safe()
        atomic_private_write(wake_path, new_bytes)
    subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist_path)], check=True)
    verify_loaded_config(True)
    pid = wait_ready(pid, True)
except BaseException as exc:
    if wake_path.read_bytes() == new_bytes:
        atomic_private_write(wake_path, wake_original)
    if service_touched:
        try:
            if process_state() is None:
                subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist_path)], check=True)
            else:
                subprocess.run(['/bin/launchctl', 'kickstart', '-k', job], check=True)
            wait_ready(pid, wake_path.read_bytes() == new_bytes)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as recovery_error:
            print(f'Connector recovery failed after {type(exc).__name__}: {recovery_error}', file=sys.stderr)
            print(f'Inspect launchctl job {job} and port {server["port"]}; backup remains at {backup}.', file=sys.stderr)
    raise
print(f'Cold-session restore is enabled for codex_desktop. Connector ready as PID {pid}.')
print(f'Backup: {backup}')
PY
