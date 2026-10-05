#!/bin/zsh
# Owner-run rollout: changes only the selected endpoint's approval transport.
set -eu
connector_root="${0:A:h:h}"
exec "$connector_root/.venv/bin/python" - "$connector_root" <<'PY'
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

root = Path(sys.argv[1])
data = Path.home() / '.local/share/local-ai-connector'
endpoint_path = data / 'claude_code.json'
original = endpoint_path.read_bytes()
endpoint = json.loads(original)
if (endpoint.get('peer') != 'claude_code' or endpoint.get('tool_profile') != 'participant'
        or not isinstance(endpoint.get('approval_token'), str) or not endpoint['approval_token']):
    raise SystemExit('The Claude participant and scoped approval credential must already exist. Nothing changed.')
label = 'dev.local-ai-connector.service'
job = f'gui/{os.getuid()}/{label}'
plist = plistlib.loads((Path.home() / 'Library/LaunchAgents' / f'{label}.plist').read_bytes())
expected = [str(root / '.venv/bin/python'), '-m', 'local_ai_connector.cli', '--data', str(data), 'serve']
if (plist.get('Label') != label or plist.get('ProgramArguments') != expected
        or plist.get('EnvironmentVariables', {}).get('PYTHONPATH') != str(root / 'src')):
    raise SystemExit('Installed service does not match this project. Nothing changed.')
with httpx.Client(base_url=endpoint['url'], trust_env=False, timeout=10) as http:
    response = http.post('/call', headers={'Authorization': 'Bearer ' + endpoint['token']}, json={'action': 'status'})
    response.raise_for_status()
    if response.json().get('self') != 'claude_code':
        raise SystemExit('Unexpected endpoint identity. Nothing changed.')
with sqlite3.connect((data / 'state.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
    if db.execute("SELECT count(*) FROM channels WHERE status IN ('pending','active')").fetchone()[0]:
        raise SystemExit('A connector task is pending or active. Finish it before maintenance.')
subprocess.run(['/bin/launchctl', 'print', job], check=True, stdout=subprocess.DEVNULL)
if endpoint_path.read_bytes() != original:
    raise SystemExit('Endpoint configuration changed during validation. Nothing changed.')
endpoint['approval_transport'] = 'host_tool_permission'
fd, temporary = tempfile.mkstemp(prefix='.claude-approval-', dir=data)
try:
    with os.fdopen(fd, 'w') as output:
        json.dump(endpoint, output, ensure_ascii=False, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, endpoint_path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
subprocess.run(['/bin/launchctl', 'kickstart', '-k', job], check=True)
with httpx.Client(base_url=endpoint['url'], trust_env=False, timeout=3) as http:
    for attempt in range(30):
        try:
            # Invalid decision checks that the new server recognizes the source
            # without approving or creating any task.
            check = http.post('/decision', headers={'Authorization': 'Bearer ' + endpoint['approval_token']},
                              json={'channel': 'deployment-nonexistent-channel', 'decision': 'accept',
                                    'source': 'host_tool_permission'})
        except httpx.ConnectError:
            time.sleep(1)
            continue
        if check.status_code == 409 and check.json().get('error') == 'not_found':
            print('Claude approval transport configured; restarted service supports the separate audit source.')
            print('Credentials, worker bindings, Gemini and Codex configuration preserved. Reconnect Claude MCP before live testing.')
            break
        raise SystemExit('Service returned an unexpected validation result; configuration is set but deployment is not verified. Return to Codex with this message.')
    else:
        raise SystemExit('Service did not become ready; configuration is set but deployment is not verified. Return to Codex.')
PY
