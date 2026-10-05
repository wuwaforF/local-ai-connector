"""Native conversation lifecycle in Antigravity's owner-enabled sidecar environment."""
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID


def rpc(method, body, env):
    base = 'http://' + env['ANTIGRAVITY_LS_ADDRESS']
    address = urlsplit(base)
    if address.hostname not in ('localhost', '127.0.0.1', '::1') or not address.port or address.path or address.username:
        raise ValueError('Expected the host-provided loopback RPC address')
    request = urllib.request.Request(base + '/exa.language_server_pb.LanguageServerService/' + method,
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
        'x-codeium-csrf-token': env['ANTIGRAVITY_CSRF_TOKEN']})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        result = json.load(response)
    if not isinstance(result, dict) or 'error' in result or 'code' in result:
        raise ValueError('Host RPC failed')
    return result


def handle(request, *, run=subprocess.run, rpc_call=rpc, env=None, clock=time.time):
    env = os.environ if env is None else env
    op, target = request['op'], request['target']
    if op not in ('create', 'native_send', 'archive', 'confirm', 'status'):
        raise ValueError('Unsupported native lifecycle operation')
    expected = {'project_id', 'workspace_uri'} | (set() if op == 'create' else {'conversation_id'})
    if not isinstance(target, dict) or set(target) != expected:
        raise ValueError('Explicit native project, workspace and conversation required')
    UUID(target['project_id'])
    if target['project_id'] != env['ANTIGRAVITY_PROJECT_ID'] or not target['workspace_uri'].startswith('file:///'):
        return {'ok': False, 'error': 'stale_target', 'detail': 'Sidecar project does not match the requested target'}
    if op != 'create':
        UUID(target['conversation_id'])
        metadata = rpc_call('GetConversationMetadata', {'conversationId': target['conversation_id']}, env)['metadata']
        if (metadata['rootConversationId'] != target['conversation_id'] or metadata['projectId'] != target['project_id']
                or target['workspace_uri'] not in metadata['workspaceUris']):
            return {'ok': False, 'error': 'stale_target', 'detail': 'Native identity mismatch'}
        if op == 'confirm':
            return {'ok': True, 'target': target}
        summary = rpc_call('GetAllCascadeTrajectories', {'excludeSubtrajectories': True}, env)['trajectorySummaries'][target['conversation_id']]
        archived = summary.get('annotations', {}).get('archived') is True
        idle = summary.get('status') == 'CASCADE_RUN_STATUS_IDLE'
        if op == 'status':
            return {'ok': True, 'state': 'idle' if idle else 'busy' if summary.get('status') == 'CASCADE_RUN_STATUS_RUNNING' else 'unknown'}
        if not idle:
            return {'ok': False, 'error': 'rejected', 'detail': 'Conversation is not idle'}
        if archived and op == 'native_send':
            return {'ok': False, 'error': 'rejected', 'detail': 'Conversation is archived'}
    deadline = request['expires_at']
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or clock() >= deadline:
        return {'ok': False, 'error': 'rejected', 'detail': 'Operation authorization expired'}
    if op == 'archive':
        if not archived:
            try:
                rpc_call('UpdateConversationAnnotations', {'cascadeIds': [target['conversation_id']],
                         'annotations': {'archived': True}, 'mergeAnnotations': True}, env)
                after = rpc_call('GetAllCascadeTrajectories', {'excludeSubtrajectories': True}, env)['trajectorySummaries'][target['conversation_id']]
                if after.get('annotations', {}).get('archived') is not True:
                    raise ValueError('Archive readback was not confirmed')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return {'ok': False, 'error': 'ambiguous', 'detail': 'Archive outcome unconfirmed: ' + type(exc).__name__}
        return {'ok': True, 'archived': True, 'target': target}
    dispatch = str(UUID(request['dispatch_id']))
    text = request['text']
    if not isinstance(text, str) or not 0 < len(text) <= 1000 or dispatch not in text:
        raise ValueError('Expected bounded connector wake instruction')
    if op == 'create':
        title = request['title']
        if not isinstance(title, str) or not 0 < len(title) <= 100:
            raise ValueError('Invalid conversation title')
        argv = ['agentapi', 'new-conversation', '--title=' + title, text]
    else:
        argv = ['agentapi', 'send-message', target['conversation_id'], text]
    try:
        result = run(argv, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            raise ValueError('agentapi returned nonzero status')
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict) or 'error' in reply or not isinstance(reply.get('response'), dict):
            raise ValueError('agentapi did not report success')
        if op == 'create':
            conversation = str(UUID(reply['response']['newConversation']['conversationId']))
            actual = {**target, 'conversation_id': conversation}
            metadata = rpc_call('GetConversationMetadata', {'conversationId': conversation}, env)['metadata']
            if (metadata['rootConversationId'] != conversation or metadata['projectId'] != target['project_id']
                    or target['workspace_uri'] not in metadata['workspaceUris']):
                return {'ok': False, 'error': 'ambiguous', 'detail': 'Created conversation metadata mismatch: ' + conversation}
            return {'ok': True, 'target': actual}
        return {'ok': True, 'accepted': True, 'host_ref': dispatch}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        return {'ok': False, 'error': 'ambiguous', 'detail': 'Native operation outcome unconfirmed: ' + type(exc).__name__}


if __name__ == '__main__':
    try:
        response = handle(json.load(sys.stdin))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        response = {'ok': False, 'error': 'unavailable', 'detail': 'Native preflight failed: ' + type(exc).__name__}
    print(json.dumps(response))
