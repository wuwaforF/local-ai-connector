"""Credential-free stdlib-only MCP wire probe, for protocol comparison."""
import argparse
import json
import os
import re
import secrets
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--mode', choices=('modern', 'legacy'), required=True)
parser.add_argument('--evidence', required=True)
parser.add_argument('--forward-progress-token', action='store_true')
options = parser.parse_args()
modern = options.mode == 'modern'
info = {'name': 'Isolated Wire Form Probe', 'version': '1.0'}
pending = {}
protocol = None
capabilities = {}


def record(event, **fields):
    fd = os.open(options.evidence, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write(json.dumps({'event': event, 'time': time.time(), 'mode': options.mode, **fields}) + '\n')


def send(message):
    print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)


def result(request_id, payload):
    if modern:
        payload.setdefault('resultType', 'complete')
        if 'tools' in payload or 'supportedVersions' in payload:
            payload.update(cacheScope='private', ttlMs=0)
        payload['_meta'] = {'io.modelcontextprotocol/serverInfo': info}
    send({'id': request_id, 'result': payload})


def finish(request_id, state, response):
    observed = {'state': 'diagnostic_complete', 'probe_id': state['args']['probe_id'], 'field_type': state['args']['field_type'],
                'action': response.get('action'), 'response_error': 'error' in response,
                'elapsed_ms': round((time.monotonic() - state['started']) * 1000, 3),
                'protocol': state['protocol'], 'form_present': 'form' in state['elicitation'],
                'host_meta_present': response.get('_meta') is not None}
    record('form_returned', **observed)
    result(request_id, {'content': [{'type': 'text', 'text': json.dumps(observed)}], 'isError': False})


for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get('id')
    method = message.get('method')
    params = message.get('params') or {}
    if method is None and request_id in pending:
        state = pending.pop(request_id)
        finish(state['request_id'], state, message.get('result', {'error': True}))
        continue
    if method and request_id is None:
        continue
    meta = params.get('_meta') or {}
    if modern:
        protocol = meta.get('io.modelcontextprotocol/protocolVersion')
        capabilities = meta.get('io.modelcontextprotocol/clientCapabilities', {})
    if method == 'server/discover':
        record('discovery', protocol=protocol, form_present='form' in capabilities.get('elicitation', {}))
        if modern:
            result(request_id, {'supportedVersions': ['2026-07-28'], 'capabilities': {'tools': {}}})
        else:
            send({'id': request_id, 'error': {'code': -32601, 'message': 'This probe supports legacy initialize only'}})
    elif method == 'initialize' and not modern:
        protocol = '2025-11-25'
        capabilities = params.get('capabilities', {})
        record('initialized', protocol=protocol, form_present='form' in capabilities.get('elicitation', {}))
        result(request_id, {'protocolVersion': protocol, 'capabilities': {'tools': {}}, 'serverInfo': info})
    elif method == 'tools/list':
        result(request_id, {'tools': [{'name': 'probe_form',
            'description': 'Show one diagnostic form; no tasks or permissions are created. Let the human fill it. Report actual result.',
            'inputSchema': {'type': 'object', 'properties': {'field_type': {'type': 'string', 'enum': ['text', 'boolean']}, 'probe_id': {'type': 'string'}}, 'required': ['field_type', 'probe_id']},
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}}]})
    elif method == 'tools/call':
        args = params.get('arguments', {})
        if params.get('name') != 'probe_form' or args.get('field_type') not in ('text', 'boolean') or not re.fullmatch('[a-zA-Z0-9_-]{1,80}', args.get('probe_id', '')):
            send({'id': request_id, 'error': {'code': -32602, 'message': 'Invalid diagnostic arguments'}})
            continue
        if modern and 'inputResponses' in params:
            state = pending.pop(params.get('requestState'), None)
            response = params['inputResponses'].get('probe')
            if state is None or state['args'] != args or not isinstance(response, dict):
                send({'id': request_id, 'error': {'code': -32602, 'message': 'Invalid probe response binding'}})
                continue
            finish(request_id, state, response)
            continue
        schema = {'type': 'object', 'properties': {'value': {'type': 'string' if args['field_type'] == 'text' else 'boolean'}}, 'required': ['value']}
        elicitation = {'mode': 'form', 'message': 'Diagnostic only; no task or permission. Enter DIAGNOSTIC for text, or select Yes for Boolean.', 'requestedSchema': schema}
        if options.forward_progress_token and 'progressToken' in meta:
            elicitation['_meta'] = {'progressToken': meta['progressToken']}
        key = 'probe-' + secrets.token_hex(12)
        pending[key] = {'args': args, 'started': time.monotonic(), 'request_id': request_id, 'protocol': protocol, 'elicitation': capabilities.get('elicitation', {})}
        record('form_requested', probe_id=args['probe_id'], field_type=args['field_type'], schema=schema, protocol=protocol, form_present='form' in capabilities.get('elicitation', {}), parent_progress_token_present='progressToken' in meta, forwarded_progress_token='_meta' in elicitation)
        if modern:
            result(request_id, {'resultType': 'input_required', 'inputRequests': {'probe': {'method': 'elicitation/create', 'params': elicitation}}, 'requestState': key})
        else:
            send({'id': key, 'method': 'elicitation/create', 'params': elicitation})
    elif method == 'ping':
        result(request_id, {})
    else:
        send({'id': request_id, 'error': {'code': -32601, 'message': 'Method not supported by diagnostic'}})
