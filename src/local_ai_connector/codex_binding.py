"""Metadata-only discovery through the configured official Codex App Server."""
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

from .core import require, ConnectorError
from .conversations import canonical
from .claude_binding import label

VERIFICATION_TIMEOUT_SECONDS = 5


class CodexCatalog:
    def __init__(self, executable):
        require(isinstance(executable, str) and Path(executable).is_absolute()
                and Path(executable).is_file(), 'codex_binding_unavailable',
                'Configure the installed official Codex executable before enabling quick binding.')
        self.executable = executable

    async def _read(self, method, params):
        require(method in ('thread/list', 'thread/read'), 'invalid_binding_query', 'Metadata queries only.')
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable, 'app-server', '--listen', 'stdio://',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024)
        except OSError as exc:
            raise ConnectorError('codex_binding_unavailable',
                                 'Codex metadata server could not start: ' + type(exc).__name__) from exc
        try:
            async with asyncio.timeout(20):
                for request in (
                    {'id': 1, 'method': 'initialize', 'params': {'clientInfo': {
                        'name': 'local_ai_connector_catalog', 'version': '0.1.0'}}},
                    {'method': 'initialized', 'params': {}},
                    {'id': 2, 'method': method, 'params': params},
                ):
                    process.stdin.write((json.dumps(request) + '\n').encode())
                    await process.stdin.drain()
                    if 'id' not in request:
                        continue
                    while True:
                        raw = await process.stdout.readline()
                        require(bool(raw), 'codex_binding_unavailable', 'Codex metadata server closed before replying.')
                        response = json.loads(raw)
                        require(isinstance(response, dict), 'invalid_binding_response', 'Invalid metadata response.')
                        require(not ('method' in response and 'id' in response),
                                'unexpected_host_request', 'Metadata discovery never answers permission requests.')
                        if response.get('id') != request['id']:
                            continue
                        require('error' not in response and isinstance(response.get('result'), dict),
                                'codex_binding_unavailable', 'Codex metadata query failed; no chat was resumed.')
                        if request['id'] == 2:
                            return response['result']
                        break
        except (TimeoutError, OSError, ValueError) as exc:
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError('codex_binding_unavailable',
                                 'Codex metadata discovery failed: ' + type(exc).__name__) from exc
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), 1)
                except (TimeoutError, asyncio.CancelledError) as exc:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(asyncio.shield(process.wait()), 1)
                    if isinstance(exc, asyncio.CancelledError):
                        raise

    @staticmethod
    def row(thread):
        require(isinstance(thread, dict), 'invalid_binding_response', 'Invalid thread metadata.')
        tid, cwd = thread.get('id'), thread.get('cwd')
        try:
            valid_id = isinstance(tid, str) and str(UUID(tid)) == tid
        except ValueError:
            valid_id = False
        require(valid_id and isinstance(cwd, str)
                and Path(cwd).is_absolute(), 'invalid_binding_response', 'Exact local identity and cwd are required.')
        require(thread.get('ephemeral') is False and thread.get('source') in ('vscode', 'cli')
                and thread.get('hostId', 'local') == 'local',
                'unsupported_codex_chat', 'Select a persisted local interactive Codex chat.')
        snapshot = {'thread_id': tid, 'workspace': cwd,
                    'title': label(thread.get('name') or tid), 'updated_at': thread.get('updatedAt')}
        revision = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
        return {k: snapshot[k] for k in ('thread_id', 'workspace', 'title')} | {'selection_revision': revision}

    async def catalog(self, ingress, *, cursor=None, workspace=None):
        params = {'limit': 20, 'archived': False, 'useStateDbOnly': True}
        if cursor is not None:
            require(isinstance(cursor, str) and 0 < len(cursor) <= 4096,
                    'invalid_binding_query', 'Use the returned pagination cursor.')
            params['cursor'] = cursor
        if workspace is not None:
            params['cwd'] = workspace
        result = await self._read('thread/list', params)
        require(isinstance(result.get('data'), list), 'invalid_binding_response', 'Invalid thread list.')
        choices = []
        for thread in result['data']:
            require(isinstance(thread, dict), 'invalid_binding_response', 'Invalid thread metadata.')
            if thread.get('id') == ingress:
                continue
            if thread.get('ephemeral') is not False or thread.get('source') not in ('vscode', 'cli'):
                continue
            choices.append(self.row(thread))
        return {'status': 'catalog_ready', 'choices': choices, 'next_cursor': result.get('nextCursor'),
                'readiness': 'Persisted metadata only; this process cannot establish Desktop idleness.'}

    async def verify(self, selected, ingress, *, exact=True, refresh=False):
        try:
            async with asyncio.timeout(VERIFICATION_TIMEOUT_SECONDS):
                return await self._verify(selected, ingress, exact=exact, refresh=refresh)
        except TimeoutError as exc:
            raise ConnectorError('codex_binding_unavailable', 'Selected-chat metadata verification exceeded its bounded budget.') from exc

    async def _verify(self, selected, ingress, *, exact=True, refresh=False):
        require(isinstance(selected, dict) and set(selected) == {
            'thread_id', 'workspace', 'title', 'selection_revision'},
            'invalid_codex_selection', 'Carry the exact selected catalog row, not an inferred identity.')
        require(all(isinstance(value, str) and value for value in selected.values())
                and len(selected['selection_revision']) == 64,
                'invalid_codex_selection', 'Selected metadata fields must be nonempty strings.')
        require(selected['thread_id'] != ingress, 'invalid_codex_selection', 'Do not select the ingress itself.')
        result = await self._read('thread/read', {'threadId': selected['thread_id'], 'includeTurns': False})
        row = self.row(result.get('thread'))
        require((row['thread_id'], row['workspace']) == (selected['thread_id'], selected['workspace']),
                'codex_selection_stale', 'The selected chat identity or workspace changed.')
        if exact:
            require(row == selected, 'codex_selection_stale', 'Refresh the catalog and obtain a new selection.')
        cursor, seen = None, set()
        for _ in range(100):
            page = await self.catalog(ingress, cursor=cursor, workspace=row['workspace'])
            match = next((item for item in page['choices'] if item['thread_id'] == row['thread_id']), None)
            if match is not None:
                require(match == selected if exact else match['workspace'] == selected['workspace'],
                        'codex_selection_stale', 'Latest listed metadata changed during verification.')
                return match if refresh else selected
            cursor = page['next_cursor']
            if cursor is None:
                break
            require(cursor not in seen, 'invalid_binding_response', 'Repeated metadata cursor.')
            seen.add(cursor)
        raise ConnectorError('codex_selection_stale', 'Selected chat is archived, unavailable or cannot be verified.')
