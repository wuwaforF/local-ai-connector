import asyncio
import json
import pytest

from local_ai_connector.codex_binding import CodexCatalog
from local_ai_connector.core import ConnectorError
from test_conversations import SESSION, CHILD


def metadata(**changes):
    return {'id': CHILD, 'name': 'Selected chat', 'cwd': '/target', 'source': 'vscode',
            'ephemeral': False, 'updatedAt': 100, 'status': {'type': 'notLoaded'}, **changes}


@pytest.mark.parametrize('changes', [dict(id='invalid'), dict(cwd='relative'),
    dict(ephemeral=True), dict(source='subAgent'), dict(hostId='remote')])
def test_catalog_rejects_unverifiable_local_identity(changes):
    with pytest.raises(ConnectorError):
        CodexCatalog.row(metadata(**changes))


@pytest.mark.parametrize('unavailable', ['not_executable', 'removed'])
async def test_metadata_server_startup_failure_reports_unavailable(tmp_path, unavailable):
    executable = tmp_path / 'codex'
    executable.touch()
    source = CodexCatalog(str(executable))
    if unavailable == 'removed':
        executable.unlink()
    with pytest.raises(ConnectorError) as error:
        await source.catalog(SESSION)
    assert error.value.code == 'codex_binding_unavailable'


@pytest.mark.parametrize('invalid_row', [None, 'not a thread', []])
async def test_catalog_rejects_nonobject_thread_rows(tmp_path, invalid_row):
    executable = tmp_path / 'codex'
    executable.touch()
    source = CodexCatalog(str(executable))
    async def read(method, params):
        return {'data': [invalid_row], 'nextCursor': None}
    source._read = read
    with pytest.raises(ConnectorError) as error:
        await source.catalog(SESSION)
    assert error.value.code == 'invalid_binding_response'


async def test_catalog_keeps_desktop_source_and_paginates_without_reading_turns(tmp_path):
    executable = tmp_path / 'codex'
    executable.touch()
    source = CodexCatalog(str(executable))
    calls = []
    async def read(method, params):
        calls.append((method, params))
        return {'data': [metadata(id=SESSION), metadata()], 'nextCursor': 'page-two'}
    source._read = read
    result = await source.catalog(SESSION)
    assert len(result['choices']) == 1 and result['choices'][0]['thread_id'] == CHILD
    assert result['next_cursor'] == 'page-two'
    assert 'notLoaded' not in result['choices'][0]
    assert calls == [('thread/list', {'limit': 20, 'archived': False, 'useStateDbOnly': True})]


@pytest.mark.parametrize('change,exact', [(dict(cwd='/other'), False),
    (dict(name='Renamed'), True), (dict(updatedAt=101), True)])
async def test_catalog_requires_fresh_selected_identity(tmp_path, change, exact):
    executable = tmp_path / 'codex'
    executable.touch()
    source = CodexCatalog(str(executable))
    selected = source.row(metadata())
    async def read(method, params):
        return {'thread': metadata(**change)}
    source._read = read
    with pytest.raises(ConnectorError, match='changed|Refresh'):
        await source.verify(selected, SESSION, exact=exact)


async def test_revalidation_allows_new_answer_timestamp_but_requires_unarchived_membership(tmp_path):
    executable = tmp_path / 'codex'
    executable.touch()
    source = CodexCatalog(str(executable))
    selected = source.row(metadata())
    archived = False
    calls = []
    async def read(method, params):
        calls.append((method, params))
        if method == 'thread/read':
            assert params['includeTurns'] is False
            return {'thread': metadata(updatedAt=200)}
        return {'data': [] if archived else [metadata(updatedAt=200)], 'nextCursor': None}
    source._read = read
    assert await source.verify(selected, SESSION, exact=False) == selected
    archived = True
    with pytest.raises(ConnectorError, match='archived'):
        await source.verify(selected, SESSION, exact=False)
    assert all(method in ('thread/read', 'thread/list') for method, _ in calls)


async def test_selected_chat_is_not_inferred_from_ingress_or_extra_model_fields(tmp_path):
    executable = tmp_path / 'codex'
    executable.touch()
    source = CodexCatalog(str(executable))
    selected = source.row(metadata(id=SESSION))
    with pytest.raises(ConnectorError, match='ingress'):
        await source.verify(selected, SESSION)
    with pytest.raises(ConnectorError, match='exact'):
        await source.verify({**selected, 'model': 'chosen-by-caller'}, SESSION)


async def test_final_list_evidence_cannot_override_newer_selected_revision(tmp_path):
    executable = tmp_path / 'codex'
    executable.touch()
    source = CodexCatalog(str(executable))
    selected = source.row(metadata())
    async def read(method, params):
        if method == 'thread/read':
            return {'thread': metadata()}
        return {'data': [metadata(name='Changed after read')], 'nextCursor': None}
    source._read = read
    with pytest.raises(ConnectorError, match='Latest listed'):
        await source.verify(selected, SESSION)


async def test_saved_target_refresh_returns_final_list_row_for_same_identity_and_workspace(tmp_path):
    executable = tmp_path / 'codex'
    executable.touch()
    source = CodexCatalog(str(executable))
    selected = source.row(metadata())
    latest = metadata(name='Current title', updatedAt=200)
    async def read(method, params):
        return {'thread': metadata()} if method == 'thread/read' else {'data': [latest], 'nextCursor': None}
    source._read = read
    refreshed = await source.verify(selected, SESSION, exact=False, refresh=True)
    assert refreshed == source.row(latest)
    assert refreshed['thread_id'] == selected['thread_id'] and refreshed['workspace'] == selected['workspace']
    assert refreshed['selection_revision'] != selected['selection_revision']


async def test_verification_timeout_kills_and_reaps_process_even_during_cleanup(tmp_path, monkeypatch):
    import local_ai_connector.codex_binding as module
    executable = tmp_path / 'codex'
    executable.touch()
    source = CodexCatalog(str(executable))
    class Process:
        def __init__(self):
            self.returncode = None
            self.stdin = self.stdout = self
            self.responses = [json.dumps({'id': 1, 'result': {}}).encode() + b'\n',
                json.dumps({'id': 2, 'result': {'thread': metadata()}}).encode() + b'\n']
            self.killed = self.reaped = self.terminated = False
            self.done = asyncio.Event()
        def write(self, _value):
            pass
        async def drain(self):
            pass
        async def readline(self):
            return self.responses.pop(0)
        def terminate(self):
            self.terminated = True
        def kill(self):
            self.killed = True
            self.returncode = -9
            self.done.set()
        async def wait(self):
            await self.done.wait()
            self.reaped = True
            return self.returncode
    process = Process()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(module, 'VERIFICATION_TIMEOUT_SECONDS', 0.02)
    with pytest.raises(ConnectorError, match='bounded budget'):
        await source.verify(source.row(metadata()), SESSION)
    assert process.terminated and process.killed and process.reaped
