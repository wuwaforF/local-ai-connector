import pytest

from local_ai_connector.core import Broker, ConnectorError
from local_ai_connector.conversations import BINDING_MESSAGE
from test_conversations import CHILD, REGISTRATION, SESSION


SELECTED = {
    'thread_id': CHILD,
    'workspace': '/target',
    'title': 'Original title',
    'selection_revision': 'a' * 64,
}


class FakeCatalog:
    def __init__(self):
        self.current = dict(SELECTED)
        self.calls = []
        self.failure = None
        self.after_verify = None

    async def verify(self, selected, ingress, exact=True, refresh=False):
        self.calls.append((dict(selected), ingress, exact, refresh))
        if self.failure:
            raise self.failure
        if exact and selected != self.current:
            raise ConnectorError('stale_conversation_config', 'Selected chat metadata changed.')
        if self.after_verify:
            callback, self.after_verify = self.after_verify, None
            result = callback()
            if hasattr(result, '__await__'):
                await result
        if selected.get('thread_id') != self.current.get('thread_id'):
            raise ConnectorError('stale_conversation_config', 'Selected chat identity changed.')
        if selected.get('workspace') != self.current.get('workspace'):
            raise ConnectorError('stale_conversation_config', 'Selected chat workspace changed.')
        return dict(self.current) if refresh else dict(selected)

    def verify_hook(self, selected, ingress):
        if selected != self.current or ingress != SESSION:
            raise ConnectorError('stale_conversation_config', 'Selected chat metadata changed.')


@pytest.fixture
def context(tmp_path):
    now = [100]
    catalog = FakeCatalog()
    path = tmp_path / 'saved-bindings.sqlite3'

    def make_broker():
        broker = Broker(path, clock=lambda: now[0], conversations={'codex': dict(REGISTRATION)},
                        codex_catalog=catalog)
        for peer in ('codex', 'antigravity', 'claude'):
            broker.register(peer, peer + '-token')
        return broker

    broker = make_broker()
    yield {'broker': broker, 'catalog': catalog, 'now': now, 'path': path,
           'make_broker': make_broker}
    broker.close()


async def request_binding(broker, *, peer='antigravity', target='codex', key='binding',
                          selected=None):
    return await broker.open(peer, target, BINDING_MESSAGE, key,
                             selected_conversation=dict(SELECTED if selected is None else selected),
                             binding_only=True)


async def approve_binding(broker, request, peer='antigravity'):
    return await broker.decide(request['id'], True,
                               host_approval=(peer, 'accept'))


async def test_approval_saves_requester_scoped_preference_without_task_or_reservation(context):
    broker = context['broker']
    request = await request_binding(broker)
    await approve_binding(broker, request)

    assert broker.delegation_result('antigravity', request['id'])['state'] == 'bound'
    saved = await broker.saved_codex_target('antigravity', 'codex')
    assert saved['state'] == 'binding_ready'
    assert saved['target'] == 'codex'
    assert saved['selected_conversation'] == SELECTED
    assert broker.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
    assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0
    assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 1


async def test_admin_snapshot_shows_pending_and_approved_binding_scope(context):
    broker = context['broker']
    request = await request_binding(broker)
    pending = broker.snapshot()['channels'][0]
    assert pending['status'] == 'pending'
    assert pending['conversation']['selected'] == SELECTED
    assert pending['conversation']['binding_only'] is True

    await approve_binding(broker, request)
    approved = broker.snapshot()['channels'][0]
    assert approved['status'] == 'closed'
    assert approved['conversation']['selected'] == SELECTED
    assert approved['conversation']['binding_only'] is True


async def test_saved_preference_outlives_binding_request_and_returns_refreshed_metadata(context):
    broker = context['broker']
    request = await request_binding(broker)
    await approve_binding(broker, request)
    context['now'][0] += 3601
    broker.expire()
    context['catalog'].current = {**SELECTED, 'title': 'Renamed in Desktop',
                                  'selection_revision': 'b' * 64}

    saved = await broker.saved_codex_target('antigravity', 'codex')

    assert saved['selected_conversation'] == context['catalog'].current
    assert context['catalog'].calls[-1] == (SELECTED, SESSION, False, True)
    result = broker.delegation_result('antigravity', request['id'])
    assert result['state'] == 'bound'


async def test_binding_preference_is_requester_scoped_and_missing_is_explicit(context):
    request = await request_binding(context['broker'], peer='antigravity')
    await approve_binding(context['broker'], request)

    with pytest.raises(ConnectorError) as error:
        await context['broker'].saved_codex_target('claude', 'codex')

    assert error.value.code == 'codex_binding_missing'


async def test_same_target_can_be_saved_by_two_requesters_without_crossing_scopes(context):
    broker = context['broker']
    first = await request_binding(broker, peer='antigravity', key='first')
    second = await request_binding(broker, peer='claude', key='second')
    await approve_binding(broker, first, 'antigravity')
    await approve_binding(broker, second, 'claude')

    assert broker.delegation_result('antigravity', first['id'])['state'] == 'bound'
    assert broker.delegation_result('claude', second['id'])['state'] == 'bound'
    assert (await broker.saved_codex_target('antigravity', 'codex'))['state'] == 'binding_ready'
    assert (await broker.saved_codex_target('claude', 'codex'))['state'] == 'binding_ready'
    assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0


async def test_replacing_preference_supersedes_prior_binding_request(context):
    broker = context['broker']
    first = await request_binding(broker, key='first')
    await approve_binding(broker, first)
    second_selection = {**SELECTED, 'thread_id': '33333333-3333-4333-8333-333333333333',
                        'title': 'Second chat', 'selection_revision': 'c' * 64}
    context['catalog'].current = second_selection
    second = await request_binding(broker, key='second', selected=second_selection)
    await approve_binding(broker, second)

    assert broker.delegation_result('antigravity', first['id'])['state'] == 'superseded'
    assert broker.delegation_result('antigravity', second['id'])['state'] == 'bound'
    await approve_binding(broker, first)
    saved = await broker.saved_codex_target('antigravity', 'codex')
    assert saved['selected_conversation'] == second_selection
    assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 2


async def test_revoking_obsolete_binding_request_preserves_current_preference(context):
    broker = context['broker']
    first = await request_binding(broker, key='first')
    await approve_binding(broker, first)
    second_selection = {**SELECTED, 'thread_id': '33333333-3333-4333-8333-333333333333',
                        'title': 'Current chat', 'selection_revision': 'f' * 64}
    context['catalog'].current = second_selection
    second = await request_binding(broker, key='second', selected=second_selection)
    await approve_binding(broker, second)

    await broker.revoke(first['id'])
    saved = await broker.saved_codex_target('antigravity', 'codex')
    assert saved['selected_conversation'] == second_selection

    await broker.revoke(second['id'])
    with pytest.raises(ConnectorError) as error:
        await broker.saved_codex_target('antigravity', 'codex')
    assert error.value.code == 'codex_binding_missing'


async def test_same_target_binding_requests_do_not_reserve_tasks_but_task_routes_still_do(context):
    broker = context['broker']
    first = await request_binding(broker, peer='antigravity', key='first')
    second = await request_binding(broker, peer='claude', key='second')
    await approve_binding(broker, first, 'antigravity')
    await approve_binding(broker, second, 'claude')

    task = await broker.open('antigravity', 'codex', 'Task body', 'task',
                             selected_conversation=SELECTED)
    with pytest.raises(ConnectorError) as error:
        await broker.open('claude', 'codex', 'Another task', 'task-2',
                          selected_conversation=SELECTED)

    assert error.value.code == 'codex_chat_reserved'
    assert task['conversation']['selected'] == SELECTED


async def test_replacing_saved_preference_does_not_change_existing_task_route(context):
    broker = context['broker']
    initial_binding = await request_binding(broker, key='initial-binding')
    await approve_binding(broker, initial_binding)
    task = await broker.open('antigravity', 'codex', 'Task pinned to first chat', 'task',
                             selected_conversation=SELECTED)
    await approve_binding(broker, task)
    question = (await broker.receive('codex', task['id'], timeout=0))['messages'][0]
    second_selection = {**SELECTED, 'thread_id': '33333333-3333-4333-8333-333333333333',
                        'title': 'Second chat', 'selection_revision': 'e' * 64}
    context['catalog'].current = second_selection
    replacement = await request_binding(broker, key='replacement', selected=second_selection)
    await approve_binding(broker, replacement)

    assert broker.conversations.existing_route(task['id']) == SELECTED
    assert question['native_execution']['arguments'] == {
        'threadId': CHILD, 'hostId': 'local', 'prompt': 'Task pinned to first chat'}
    assert broker.delegation_result('antigravity', initial_binding['id'])['state'] == 'superseded'


@pytest.mark.parametrize('decision', ['decline', 'cancel'])
async def test_decline_or_cancel_does_not_save_preference(context, decision):
    broker = context['broker']
    request = await request_binding(broker)
    await broker.decide(request['id'], False, host_approval=('antigravity', decision))

    with pytest.raises(ConnectorError) as error:
        await broker.saved_codex_target('antigravity', 'codex')

    assert error.value.code == 'codex_binding_missing'
    assert broker.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0


async def test_wrong_requester_approval_cannot_revoke_or_bind_selection(context):
    broker = context['broker']
    request = await request_binding(broker)
    context['catalog'].failure = ConnectorError('stale_conversation_config', 'Selection is stale.')

    with pytest.raises(ConnectorError) as error:
        await broker.decide(request['id'], True, host_approval=('claude', 'accept'))

    assert error.value.code == 'forbidden'
    assert broker.channel(request['id'])['status'] == 'pending'
    assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 0


async def test_stale_initial_selection_is_revoked_and_never_saved(context):
    broker = context['broker']
    request = await request_binding(broker)
    context['catalog'].failure = ConnectorError('stale_conversation_config', 'Selection is stale.')

    with pytest.raises(ConnectorError) as error:
        await approve_binding(broker, request)

    assert error.value.code == 'stale_conversation_config'
    assert broker.channel(request['id'])['status'] == 'revoked'
    assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 0
    with pytest.raises(ConnectorError) as missing:
        await broker.saved_codex_target('antigravity', 'codex')
    assert missing.value.code == 'codex_binding_missing'


@pytest.mark.parametrize('drift', ['source', 'thread_id', 'workspace'])
async def test_saved_target_metadata_identity_or_workspace_drift_fails(context, drift):
    broker = context['broker']
    request = await request_binding(broker)
    await approve_binding(broker, request)
    if drift == 'source':
        context['catalog'].after_verify = lambda: context['broker'].conversations.registrations[
            'codex'].__setitem__('session_id', '44444444-4444-4444-8444-444444444444')
    elif drift == 'thread_id':
        context['catalog'].current = {**SELECTED,
                                      'thread_id': '33333333-3333-4333-8333-333333333333'}
    else:
        context['catalog'].current = {**SELECTED, 'workspace': '/moved'}

    with pytest.raises(ConnectorError):
        await broker.saved_codex_target('antigravity', 'codex')


async def test_saved_target_rechecks_preference_after_metadata_refresh(context):
    broker = context['broker']
    request = await request_binding(broker)
    await approve_binding(broker, request)
    second_selection = {**SELECTED, 'title': 'Updated metadata',
                        'selection_revision': 'd' * 64}
    async def replace_preference():
        context['catalog'].current = second_selection
        replacement = await request_binding(broker, key='replacement', selected=second_selection)
        await approve_binding(broker, replacement)

    context['catalog'].after_verify = replace_preference

    with pytest.raises(ConnectorError):
        await broker.saved_codex_target('antigravity', 'codex')

    result = await broker.saved_codex_target('antigravity', 'codex')
    assert result['selected_conversation'] == second_selection


async def test_binding_request_replay_has_one_audit_and_binding_has_no_task_operations(context):
    broker = context['broker']
    first = await request_binding(broker, key='stable')
    replay = await request_binding(broker, key='stable')
    assert replay['id'] == first['id']
    await approve_binding(broker, first)
    assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 1
    assert broker.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
    assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0


@pytest.mark.parametrize('first_binding', [True, False])
async def test_task_and_binding_request_cannot_reuse_each_others_open_key(context, first_binding):
    broker = context['broker']
    if first_binding:
        await request_binding(broker, key='shared-key')
        with pytest.raises(ConnectorError) as error:
            await broker.open('antigravity', 'codex', 'Actual task', 'shared-key',
                              selected_conversation=SELECTED)
    else:
        await broker.open('antigravity', 'codex', 'Actual task', 'shared-key',
                          selected_conversation=SELECTED)
        with pytest.raises(ConnectorError) as error:
            await request_binding(broker, key='shared-key')
    assert error.value.code == 'idempotency_conflict'


async def test_binding_request_cannot_send_continue_or_archive_task_work(context):
    broker = context['broker']
    request = await request_binding(broker)
    await approve_binding(broker, request)

    with pytest.raises(ConnectorError):
        await broker.send('antigravity', request['id'], 'question', 'Task', 'task-message')
    assert (await broker.receive('codex', request['id'], timeout=0))['messages'] == []
    with pytest.raises(ConnectorError):
        await broker.continue_channel('antigravity', request['id'], 'Continue', 'continue')
    with pytest.raises(ConnectorError):
        await broker.archive_open('antigravity', request['id'], CHILD, 'archive')

    assert broker.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
    assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0


async def test_saved_preference_survives_broker_restart(context):
    broker = context['broker']
    request = await request_binding(broker)
    await approve_binding(broker, request)
    restarted = context['make_broker']()
    try:
        saved = await restarted.saved_codex_target('antigravity', 'codex')
        assert saved['selected_conversation'] == SELECTED
        assert restarted.delegation_result('antigravity', request['id'])['state'] == 'bound'
    finally:
        restarted.close()
