#!/bin/zsh
# Run in macOS Terminal when neither worker has a pending or active connector task.
set -eu
connector_root="${0:A:h:h}"
exec "$connector_root/.venv/bin/python" - "$connector_root" "$@" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shlex
import sqlite3
import subprocess
import sys
import time

import httpx

root = Path(sys.argv[1])
if sys.argv[2:]:
    raise SystemExit('Usage: enable-peer-chat-approval.command')
sys.path.insert(0, str(root / 'src'))
from local_ai_connector.registry import enable_chat_approval

data = Path.home() / '.local/share/local-ai-connector'
peers = ['claude_code', 'gemini']
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
server_path = data / 'server.json'
server = json.loads(server_path.read_text())
if any(peer not in server['peers'] for peer in peers):
    raise SystemExit('Both dedicated workers must already be registered. Nothing changed.')
watched = [server_path, data / 'wakeup.json', data / 'gpt.json', *(data / f'{peer}.json' for peer in peers)]
before = {path: path.read_bytes() for path in watched}


def idle():
    with sqlite3.connect((data / 'state.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        if db.execute("SELECT count(*) FROM channels WHERE status IN ('pending','active')").fetchone()[0]:
            raise RuntimeError('A connector task is pending or active. Finish it before maintenance.')


def process_state():
    """Require every listener to be the exact launchd-managed service process."""
    listing = subprocess.check_output(['/bin/launchctl', 'list'], text=True)
    jobs = [line.split() for line in listing.splitlines() if len(line.split()) == 3 and line.split()[2] == label]
    if len(jobs) > 1:
        raise RuntimeError('Multiple matching launchd jobs were reported')
    pid = jobs[0][0] if jobs and jobs[0][0].isdigit() else None
    result = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{server["port"]}', '-sTCP:LISTEN', '-t'],
                            capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError('Unable to inspect the service listener: ' + result.stderr.strip())
    listeners = set(result.stdout.split())
    if listeners and (pid is None or listeners != {pid}):
        raise RuntimeError('The listener is not the registered service. Nothing will be stopped.')
    if pid:
        uid = subprocess.check_output(['/bin/ps', '-p', pid, '-o', 'uid='], text=True).strip()
        command = subprocess.check_output(['/bin/ps', '-ww', '-p', pid, '-o', 'command='], text=True).strip()
        if uid != str(os.getuid()) or command != ' '.join(expected):
            raise RuntimeError('The service process identity differs from the installed definition')
    return bool(jobs), pid if listeners else None


def restore_service():
    try:
        loaded, _ = process_state()
        if not loaded:
            subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist_path)], check=True)
        for attempt in range(40):
            _, pid = process_state()
            if pid:
                try:
                    with httpx.Client(base_url=server['url'], trust_env=False, timeout=5) as http:
                        for peer in peers:
                            endpoint = json.loads((data / f'{peer}.json').read_text())
                            response = http.post('/call', headers={'Authorization': 'Bearer ' + endpoint['token']},
                                                 json={'action': 'status'})
                            response.raise_for_status()
                            if response.json().get('self') != peer:
                                raise RuntimeError('The recovered service returned a mismatched endpoint identity')
                except httpx.ConnectError:
                    time.sleep(1)
                    continue
                print(f'Shared service verified: launchd PID {pid} owns port {server["port"]}.', flush=True)
                return True
            time.sleep(1)
        raise RuntimeError('The service did not become ready within 40 seconds')
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, httpx.HTTPError) as exc:
        print(f'Service recovery is incomplete: {exc}', file=sys.stderr)
        print('Recovery: ' + shlex.join(['/bin/launchctl', 'bootstrap', domain, str(plist_path)]), file=sys.stderr)
        print('If already loaded: ' + shlex.join(['/bin/launchctl', 'kickstart', job]), file=sys.stderr)
        print('Then rerun this script to verify recovery. Do not start new tasks yet.', file=sys.stderr)
        return False


idle()
loaded, pid = process_state()
print('Enabling chat approval for Claude Code and Gemini; the shared service will restart briefly.', flush=True)
try:
    if loaded:
        subprocess.run(['/bin/launchctl', 'bootout', job], check=True)
    with (data / 'server.lock').open('a') as lock:
        for attempt in range(50):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        else:
            raise RuntimeError('The previous service has not stopped')
        idle()
        if any(path.read_bytes() != content for path, content in before.items()):
            raise RuntimeError('Configuration changed during maintenance; migration canceled')
    enable_chat_approval(data, peers, mcp_locale='en-US')
    for name in ('wakeup.json', 'gpt.json'):
        path = data / name
        if path.read_bytes() != before[path]:
            raise RuntimeError(f'Unexpected change to {name}; review before starting new tasks')
finally:
    restored = restore_service()
if not restored:
    raise SystemExit(1)
print('Chat approval is configured for Claude Code and Gemini. Existing credentials and wake-up bindings were preserved.')
print('Reload Local AI Connector in both worker apps, then return to Codex for the native approval tests.')
PY
