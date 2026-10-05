import asyncio
import json
from uuid import uuid4

import pytest

from local_ai_connector.core import Broker, ConnectorError
from local_ai_connector.native_host import NativeHost
from local_ai_connector.wakeup import AdapterError, Binding, Dispatcher, WakeStore

PROJECT = '11111111-1111-4111-8111-111111111111'
REGISTRATION = {'provider': 'antigravity_sidecar', 'project_id': PROJECT,
                'workspace_uri': 'file:///test', 'socket': '/tmp/test-native.sock'}


class Host:
    def __init__(self):
        self.calls = []
        self.busy = False
        self.error = None
        self.before_confirm = None

    async def _call(self, op, **args):
        self.calls.append((op, args))
        if self.error:
            raise self.error
        if op == 'create':
            return {'ok': True, 'target': {**args['target'], 'conversation_id': str(uuid4())}}
        if op == 'archive':
            return {'ok': True, 'target': args['target'], 'archived': True}
        return {'ok': True, 'accepted': True}

    async def confirm(self, target):
        if self.before_confirm:
            await self.before_confirm()
        return target

    async def status(self, target):
        return 'busy' if self.busy else 'idle'

    async def close(self):
        pass


@pytest.fixture
async def setup(tmp_path):
    broker = Broker(tmp_path / 'state.sqlite3', clock=lambda: 100, conversations={'ag': REGISTRATION})
    for peer in ('requester', 'ag', 'other'):
        broker.register(peer, peer)
    host = Host()
    native = NativeHost(broker, {'ag': host})
    yield broker, native, host
    await native.close()
    broker.close()


async def create(setup, *, answer=True):
    broker, native, host = setup
    task = await broker.open('requester', 'ag', 'Open a new chat and calculate 2+3', 'task', conversation_mode='new')
    await native.step()
    assert host.calls == []
    await broker.decide(task['id'], True, host_approval=('requester', 'accept'))
    await native.step()
    await native.step()
    assert len(host.calls) == 1
    assert host.calls[0][0] == 'create'
    assert task['id'] in host.calls[0][1]['text']
    assert (await broker.receive('ag', timeout=0))['messages'] == []
    question = (await broker.receive('ag', task['id'], timeout=0))['messages'][0]
    assert question['body'] == task['original'] and 'native_execution' not in question
    if answer:
        await broker.send('ag', task['id'], 'answer', '5', 'answer', question['id'])
        await broker.finish('requester', task['id'])
    return task, question, broker.conversations.describe(task['id'])['thread_id']


async def test_native_create_continue_archive_and_unregister(setup, tmp_path):
    broker, native, host = setup
    task, first, conversation = await create(setup)
    question = await broker.continue_channel('requester', task['id'], 'Add 7', 'round-two')
    await native.step()
    assert [op for op, _ in host.calls] == ['create', 'native_send']
    assert host.calls[-1][1]['target']['conversation_id'] == conversation
    message = (await broker.receive('ag', task['id'], after=first['seq'], timeout=0))['messages'][0]
    assert message['id'] == question['id']
    await broker.send('ag', task['id'], 'answer', '12', 'round-two-answer', question['id'])
    archive = await broker.archive_open('requester', task['id'], conversation, 'archive-one')
    await native.step()
    assert len(host.calls) == 2
    await broker.decide(archive['id'], True, host_approval=('requester', 'accept'))
    with pytest.raises(ConnectorError, match='archive'):
        await broker.continue_channel('requester', task['id'], 'Add 1', 'racing-round')
    with pytest.raises(ConnectorError, match='archive'):
        await broker.send('ag', archive['id'], 'answer', 'fake archive', 'fake', first['id'])
    assert (await broker.receive('ag', archive['id'], timeout=0))['messages'] == []
    await native.step()
    assert host.calls[-1][0] == 'archive'
    result = broker.delegation_result('requester', archive['id'])
    assert json.loads(result['answer']['body'])['registration_removed'] is True
    assert result['conversation']['state'] == 'archived'
    assert broker.db.execute('SELECT count(*) FROM native_conversations').fetchone()[0] == 0
    assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0
    assert broker.db.execute('SELECT count(*) FROM native_deliveries').fetchone()[0] == 0
    assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 2
    assert (await broker.archive_open('requester', task['id'], conversation, 'archive-one'))['id'] == archive['id']
    old = await broker.open('requester', 'ag', task['original'], 'task', conversation_mode='new')
    assert old['status'] == 'archived' and old['conversation']['thread_id'] == conversation
    with pytest.raises(ConnectorError, match='archived'):
        await broker.continue_channel('requester', task['id'], 'Replay', 'old')
    await native.step()
    assert len(host.calls) == 3


async def test_fixed_wake_dispatcher_never_receives_native_or_archive_tasks(setup, tmp_path):
    broker, native, host = setup
    task, _, conversation = await create(setup)
    wake = WakeStore(tmp_path / 'wake.sqlite3')
    dispatcher = Dispatcher(broker, wake, {'ag': Binding('ag', {'session': 'fixed'}, host)}, clock=lambda: 100, settle_seconds=0)
    try:
        await broker.continue_channel('requester', task['id'], 'Another round', 'round')
        assert dispatcher._candidates('ag') == ([], False)
        await native.step()
        q = (await broker.receive('ag', task['id'], timeout=0))['messages'][-1]
        await broker.send('ag', task['id'], 'answer', 'done', 'next', q['id'])
        archive = await broker.archive_open('requester', task['id'], conversation, 'archive')
        await broker.decide(archive['id'], True)
        assert dispatcher._candidates('ag') == ([], False)
        assert wake.db.execute('SELECT count(*) FROM channel_targets').fetchone()[0] == 0
    finally:
        broker.observers.remove(dispatcher.on_receive)
        wake.close()


@pytest.mark.parametrize('decision', ['decline', 'cancel'])
async def test_archive_rejection_keeps_chat_usable(setup, decision):
    broker, native, host = setup
    task, _, conversation = await create(setup)
    archive = await broker.archive_open('requester', task['id'], conversation, 'archive')
    await broker.decide(archive['id'], False, host_approval=('requester', decision))
    await native.step()
    assert [op for op, _ in host.calls] == ['create']
    await broker.continue_channel('requester', task['id'], 'More', 'more')
    assert broker.conversations.describe(task['id'])['state'] == 'active'


@pytest.mark.parametrize('kind,state', [('ambiguous', 'ambiguous'), ('rejected', 'failed')])
async def test_archive_failure_preserves_registration_and_never_claims_success(setup, kind, state):
    broker, native, host = setup
    task, _, conversation = await create(setup)
    archive = await broker.archive_open('requester', task['id'], conversation, 'archive')
    await broker.decide(archive['id'], True)
    host.error = AdapterError(kind)
    await native.step()
    await native.step()
    result = broker.delegation_result('requester', archive['id'])
    assert result['state'] == 'archive_' + state and 'answer' not in result
    assert broker.db.execute('SELECT thread_id FROM native_conversations').fetchone()[0] == conversation
    assert [op for op, _ in host.calls] == ['create', 'archive']
    if state == 'ambiguous':
        with pytest.raises(ConnectorError):
            await broker.continue_channel('requester', task['id'], 'More', 'more')


async def test_archive_requires_original_requester_exact_created_id_and_idle_task(setup):
    broker, native, host = setup
    task, question, conversation = await create(setup, answer=False)
    for peer, target in [('other', conversation), ('ag', conversation), ('requester', str(uuid4()))]:
        with pytest.raises(ConnectorError):
            await broker.archive_open(peer, task['id'], target, 'bad')
    archive = await broker.archive_open('requester', task['id'], conversation, 'archive')
    with pytest.raises(ConnectorError, match='Finish'):
        await broker.decide(archive['id'], True)
    assert broker.conversations.describe(task['id'])['state'] == 'active'
    existing = await broker.open('requester', 'ag', 'existing task', 'existing')
    with pytest.raises(ConnectorError, match='created'):
        await broker.archive_open('requester', existing['id'], conversation, 'bad-existing')


async def test_revocation_during_archive_preflight_prevents_mutation(setup):
    broker, native, host = setup
    task, _, conversation = await create(setup)
    archive = await broker.archive_open('requester', task['id'], conversation, 'archive')
    await broker.decide(archive['id'], True)
    host.before_confirm = lambda: broker.revoke(archive['id'])
    await native.step()
    await native.step()
    assert [op for op, _ in host.calls] == ['create']
    assert broker.conversations.describe(task['id'])['state'] == 'active'


async def test_host_busy_defers_archive_and_expiry_releases_reservation(setup):
    broker, native, host = setup
    task, _, conversation = await create(setup)
    archive = await broker.archive_open('requester', task['id'], conversation, 'archive')
    await broker.decide(archive['id'], True)
    host.busy = True
    await native.step()
    assert [op for op, _ in host.calls] == ['create']
    broker.clock = lambda: 4000
    await native.step()
    assert broker.conversations.describe(task['id'])['state'] == 'active'
    # Cleaning an old chat needs a fresh approval, not a live task authorization.
    retry = await broker.archive_open('requester', task['id'], conversation, 'new-archive')
    await broker.decide(retry['id'], True)
    host.busy = False
    await native.step()
    assert broker.conversations.describe(task['id'])['state'] == 'archived'


async def test_unknown_creation_is_not_repeated_after_restart(setup):
    broker, native, host = setup
    host.error = AdapterError('ambiguous')
    task = await broker.open('requester', 'ag', 'new', 'task', conversation_mode='new')
    await broker.decide(task['id'], True)
    await native.step()
    await native.close()
    replacement = NativeHost(broker, {'ag': host})
    try:
        await replacement.step()
        assert len(host.calls) == 1
        assert not broker.conversations.describe(task['id'])['created']
        assert broker.delegation_result('requester', task['id'])['state'] == 'native_execution_unconfirmed'
        assert (await broker.receive('ag', task['id'], timeout=0))['messages'] == []
    finally:
        await replacement.close()
        broker.observers.append(native.observed)


async def test_observed_followup_delivery_wins_over_late_timeout(setup):
    broker, native, host = setup
    task, _, _ = await create(setup)
    question = await broker.continue_channel('requester', task['id'], 'Follow-up', 'follow-up')
    async def delivered_then_timeout(op, **args):
        assert op == 'native_send'
        messages = (await broker.receive('ag', task['id'], after=question['seq'] - 1, timeout=0))['messages']
        assert messages[0]['id'] == question['id']
        raise AdapterError('ambiguous', 'host ack timed out after retrieval')
    host._call = delivered_then_timeout
    await native.step()
    assert broker.db.execute('SELECT state FROM native_operations WHERE question_id=?', (question['id'],)).fetchone()[0] == 'acknowledged'
    assert not broker.snapshot()['incidents']
    await broker.send('ag', task['id'], 'answer', 'actual follow-up', 'follow-answer', question['id'])
    assert broker.round_result('requester', task['id'], question['id'])['answer']['body'] == 'actual follow-up'
