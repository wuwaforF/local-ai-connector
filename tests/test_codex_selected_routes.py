import json
import sqlite3
from types import SimpleNamespace

import pytest

from local_ai_connector.core import Broker, ConnectorError
from local_ai_connector.conversations import SEND, handle_hook
from test_conversations import CHILD, REGISTRATION, SESSION, event


SELECTED = {
    'thread_id': CHILD,
    'workspace': '/target',
    'title': 'Test',
    'selection_revision': 'a' * 64,
}


class FakeCatalog:
    def __init__(self):
        self.current = dict(SELECTED)
        self.stale = False
        self.calls = []

    async def catalog(self, ingress, cursor=None):
        return {'choices': [dict(self.current)], 'cursor': cursor}

    async def verify(self, selected, ingress, exact=True, refresh=False):
        self.calls.append((dict(selected), ingress, exact))
        if self.stale or ingress != SESSION or selected != self.current:
            raise ConnectorError('stale_conversation_config', 'Selected chat metadata changed.')
        return dict(selected)

    def verify_hook(self, selected, ingress):
        if self.stale or ingress != SESSION or selected != self.current:
            raise ConnectorError('stale_conversation_config', 'Selected chat metadata changed.')


@pytest.fixture
def context(tmp_path):
    now = [100]
    catalog = FakeCatalog()
    path = tmp_path / 'selected-routes.sqlite3'

    def make_broker():
        result = Broker(path, clock=lambda: now[0], conversations={'codex': REGISTRATION},
                        codex_catalog=catalog)
        for peer in ('codex', 'antigravity', 'claude'):
            result.register(peer, peer + '-token')
        return result

    broker = make_broker()
    yield SimpleNamespace(broker=broker, catalog=catalog, now=now,
                          path=path, make_broker=make_broker)
    broker.close()


async def open_selected(broker, key='selected', *, target='codex', selected=None):
    return await broker.open('antigravity', target, 'Selected chat task', key,
                             conversation_mode='existing',
                             selected_conversation=dict(SELECTED if selected is None else selected))


def hook(broker, context, plan, *, name='PermissionRequest', tool_response=None):
    value = event(plan, name)
    if tool_response is not None:
        value['tool_response'] = tool_response
    return handle_hook(broker.db, value, 'codex', SESSION, '/worker', 100,
                       selected_verifier=context.catalog.verify_hook)


def receipt(thread_id=CHILD):
    return {'isError': False, 'content': [{'type': 'text',
            'text': json.dumps({'threadId': thread_id})}]}


async def approve(context, task):
    await context.broker.decide(task['id'], True,
                                host_approval=('antigravity', 'accept'))
    return (await context.broker.receive('codex', task['id'], timeout=0))['messages'][0]


async def execute_send(context, task, message):
    plan = message['native_execution']
    assert plan['tool'] == SEND
    assert plan['arguments'] == {'threadId': CHILD, 'hostId': 'local',
                                 'prompt': message['body']}
    hook(context.broker, context, plan)
    hook(context.broker, context, plan, name='PostToolUse', tool_response=receipt())


async def test_selected_route_sends_exact_child_and_continues_with_one_decision(context):
    broker = context.broker
    original_registrations = {peer: dict(value) for peer, value in broker.conversations.registrations.items()}
    task = await open_selected(broker)
    assert task['conversation']['mode'] == 'existing'
    assert task['conversation']['created'] is False
    assert task['conversation']['thread_id'] == CHILD
    assert task['conversation']['selected'] == SELECTED
    assert broker.conversations.existing_route(task['id']) == SELECTED

    initial = await approve(context, task)
    await execute_send(context, task, initial)
    await broker.send('codex', task['id'], 'answer', 'Native answer', 'answer-1', initial['id'])
    await broker.finish('antigravity', task['id'])
    next_question = await broker.continue_channel('antigravity', task['id'], 'Continue task', 'round-2')
    continued = (await broker.receive('codex', task['id'], after=initial['seq'], timeout=0))['messages'][0]
    await execute_send(context, task, continued)

    assert continued['native_execution']['arguments'] == {
        'threadId': CHILD, 'hostId': 'local', 'prompt': 'Continue task'}
    assert broker.conversations.existing_route(task['id']) == SELECTED
    assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 1
    assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 2
    assert broker.conversations.registrations == original_registrations
    assert next_question['channel'] == task['id']


async def test_decline_selected_route_creates_no_native_operation(context):
    task = await open_selected(context.broker)
    await context.broker.decide(task['id'], False,
                                host_approval=('antigravity', 'decline'))

    assert context.broker.channel(task['id'])['status'] == 'denied'
    assert context.broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0
    assert (await context.broker.receive('codex', task['id'], timeout=0))['messages'] == []


async def test_ingress_answer_is_rejected_until_selected_send_receipt(context):
    task = await open_selected(context.broker)
    initial = await approve(context, task)

    with pytest.raises(ConnectorError, match='receipt'):
        await context.broker.send('codex', task['id'], 'answer', 'Ingress substitute',
                                  'substitute', initial['id'])

    await execute_send(context, task, initial)
    await context.broker.send('codex', task['id'], 'answer', 'Native answer',
                              'native-answer', initial['id'])


@pytest.mark.parametrize('confirmed_thread', [None, '33333333-3333-4333-8333-333333333333'])
async def test_missing_or_wrong_send_receipt_is_ambiguous_and_cannot_be_retried(
        context, confirmed_thread):
    task = await open_selected(context.broker)
    initial = await approve(context, task)
    plan = initial['native_execution']
    hook(context.broker, context, plan)
    hook(context.broker, context, plan, name='PostToolUse',
         tool_response=receipt(confirmed_thread) if confirmed_thread else receipt_value({}))

    replay = (await context.broker.receive('codex', task['id'], timeout=0))['messages'][0]
    assert replay['native_execution']['state'] == 'ambiguous'
    assert 'arguments' not in replay['native_execution']
    with pytest.raises(ConnectorError):
        hook(context.broker, context, plan)
    with pytest.raises(ConnectorError, match='receipt'):
        await context.broker.send('codex', task['id'], 'answer', 'Unconfirmed',
                                  'unconfirmed', initial['id'])


def receipt_value(value):
    return {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(value)}]}


@pytest.mark.parametrize('retry', [
    {'selected': {**SELECTED, 'selection_revision': 'b' * 64}, 'target': 'codex'},
    {'selected': SELECTED, 'target': 'claude'},
])
async def test_request_key_cannot_be_reused_for_another_target_or_revision(context, retry):
    await open_selected(context.broker)

    with pytest.raises(ConnectorError) as error:
        await open_selected(context.broker, selected=retry['selected'], target=retry['target'])

    assert error.value.code == 'idempotency_conflict'
    assert context.broker.db.execute('SELECT count(*) FROM channels').fetchone()[0] == 1


async def test_selected_existing_chat_is_not_eligible_for_archive(context):
    task = await open_selected(context.broker)

    with pytest.raises(ConnectorError) as error:
        await context.broker.archive_open('antigravity', task['id'], CHILD, 'archive')

    assert error.value.code == 'invalid_archive_target'
    assert context.broker.conversations.existing_route(task['id']) == SELECTED
    assert context.broker.db.execute('SELECT count(*) FROM native_archives').fetchone()[0] == 0


async def test_selected_target_is_reserved_while_pending_and_active_then_reusable_after_close(context):
    broker = context.broker
    first = await open_selected(broker, 'first')
    with pytest.raises(ConnectorError) as error:
        await open_selected(broker, 'second')
    assert error.value.code == 'codex_chat_reserved'

    initial = await approve(context, first)
    with pytest.raises(ConnectorError) as error:
        await open_selected(broker, 'second')
    assert error.value.code == 'codex_chat_reserved'
    await execute_send(context, first, initial)
    await broker.send('codex', first['id'], 'answer', 'Done', 'answer-1', initial['id'])
    await broker.finish('antigravity', first['id'])
    context.now[0] += 121
    broker.expire()
    assert broker.channel(first['id'])['status'] == 'closed'

    second = await open_selected(broker, 'second')
    assert second['conversation']['thread_id'] == CHILD


async def test_selected_route_survives_broker_restart(context):
    task = await open_selected(context.broker)
    other = context.make_broker()
    try:
        assert other.conversations.existing_route(task['id']) == SELECTED
        assert other.conversations.describe(task['id'])['created'] is False
        assert other.conversations.describe(task['id'])['thread_id'] == CHILD
    finally:
        other.close()


async def test_stale_selected_metadata_revokes_pending_approval_and_frees_reservation(context):
    task = await open_selected(context.broker)
    context.catalog.stale = True

    with pytest.raises(ConnectorError) as error:
        await context.broker.decide(task['id'], True,
                                    host_approval=('antigravity', 'accept'))
    assert error.value.code == 'stale_conversation_config'
    assert context.broker.channel(task['id'])['status'] == 'revoked'
    assert context.broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 0

    replacement_selection = {**SELECTED, 'selection_revision': 'b' * 64}
    context.catalog.stale = False
    context.catalog.current = replacement_selection
    replacement = await open_selected(context.broker, 'replacement', selected=replacement_selection)
    assert replacement['conversation']['thread_id'] == CHILD


async def test_wrong_owner_cannot_revoke_stale_pending_selected_route(context):
    task = await open_selected(context.broker)
    context.catalog.stale = True

    with pytest.raises(ConnectorError) as error:
        await context.broker.decide(task['id'], True,
                                    host_approval=('claude', 'accept'))

    assert error.value.code == 'forbidden'
    assert context.broker.channel(task['id'])['status'] == 'pending'
    assert context.catalog.calls == [(SELECTED, SESSION, True)]
    assert context.broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 0


async def test_hook_metadata_verifier_can_revoke_through_independent_database_connection(context):
    task = await open_selected(context.broker)
    initial = await approve(context, task)
    plan = initial['native_execution']

    def revoke_during_verification(selected, ingress):
        assert selected == SELECTED and ingress == SESSION
        with sqlite3.connect(context.path, timeout=0.2) as other:
            other.execute("UPDATE channels SET status='revoked' WHERE id=?", (task['id'],))
        context.catalog.verify_hook(selected, ingress)

    with pytest.raises(ConnectorError) as error:
        handle_hook(context.broker.db, event(plan), 'codex', SESSION, '/worker', 100,
                    selected_verifier=revoke_during_verification)

    assert error.value.code == 'native_grant_missing'
    assert context.broker.channel(task['id'])['status'] == 'revoked'
    assert context.broker.db.execute('SELECT state FROM native_operations').fetchone()[0] == 'prepared'


async def test_uncertain_selected_operation_keeps_target_reserved(context):
    task = await open_selected(context.broker)
    initial = await approve(context, task)
    plan = initial['native_execution']
    hook(context.broker, context, plan)
    hook(context.broker, context, plan, name='PostToolUse', tool_response=receipt_value({}))
    context.broker.db.execute("UPDATE channels SET status='revoked' WHERE id=?", (task['id'],))
    context.broker.db.commit()

    with pytest.raises(ConnectorError) as error:
        await open_selected(context.broker, 'replacement')

    assert error.value.code == 'codex_chat_reserved'
    assert context.broker.db.execute('SELECT state FROM native_operations').fetchone()[0] == 'ambiguous'


async def _prepare_same_argument_competitor(context, key, *, status='active'):
    alternate = {**SELECTED, 'thread_id': '33333333-3333-4333-8333-333333333333'}
    context.catalog.current = alternate
    task = await open_selected(context.broker, key, selected=alternate)
    initial = await approve(context, task)
    plan = initial['native_execution']
    context.broker.db.execute('UPDATE native_existing_routes SET selection=? WHERE channel=?',
                              (json.dumps(SELECTED, sort_keys=True, separators=(',', ':')), task['id']))
    context.broker.db.execute('UPDATE native_operations SET arguments=? WHERE channel=?',
                              (json.dumps({'threadId': CHILD, 'hostId': 'local',
                                           'prompt': 'Selected chat task'}, sort_keys=True, separators=(',', ':')),
                               task['id']))
    if status != 'active':
        context.broker.db.execute('UPDATE channels SET status=? WHERE id=?', (status, task['id']))
    context.broker.db.commit()
    context.catalog.current = SELECTED
    plan = {**plan, 'arguments': {'threadId': CHILD, 'hostId': 'local',
                                  'prompt': 'Selected chat task'}}
    return task, plan


@pytest.mark.parametrize('inactive_status', ['expired', 'revoked'])
async def test_inactive_prepared_same_argument_grant_does_not_create_ambiguity(context, inactive_status):
    active = await open_selected(context.broker, 'active')
    initial = await approve(context, active)
    inactive, inactive_plan = await _prepare_same_argument_competitor(
        context, 'inactive', status=inactive_status)
    assert inactive_plan['arguments'] == initial['native_execution']['arguments']

    hook(context.broker, context, initial['native_execution'])

    assert context.broker.db.execute('SELECT state FROM native_operations WHERE channel=?',
                                     (active['id'],)).fetchone()[0] == 'intent'
    assert context.broker.db.execute('SELECT state FROM native_operations WHERE channel=?',
                                     (inactive['id'],)).fetchone()[0] == 'prepared'


async def test_revoked_grant_a_cannot_transfer_to_grant_b_during_metadata_verification(context):
    first = await open_selected(context.broker, 'first')
    first_message = await approve(context, first)
    second, second_plan = await _prepare_same_argument_competitor(context, 'second', status='revoked')
    assert second_plan['arguments'] == first_message['native_execution']['arguments']

    def replace_authority_during_verification(selected, ingress):
        assert selected == SELECTED and ingress == SESSION
        with sqlite3.connect(context.path) as other:
            other.execute("UPDATE channels SET status='revoked' WHERE id=?", (first['id'],))
            other.execute("UPDATE channels SET status='active',approved_at=100 WHERE id=?", (second['id'],))
        context.catalog.verify_hook(selected, ingress)

    with pytest.raises(ConnectorError) as error:
        handle_hook(context.broker.db, event(first_message['native_execution']), 'codex', SESSION,
                    '/worker', 100, selected_verifier=replace_authority_during_verification)

    assert error.value.code == 'native_grant_mismatch'
    states = dict(context.broker.db.execute('SELECT channel,state FROM native_operations'))
    assert states[first['id']] == 'prepared'
    assert states[second['id']] == 'prepared'
    assert context.broker.channel(first['id'])['status'] == 'revoked'
    assert context.broker.channel(second['id'])['status'] == 'active'


async def test_created_child_continuation_is_blocked_by_selected_route_reservation(context):
    selected = await open_selected(context.broker, 'selected')
    created = await context.broker.open('antigravity', 'codex', 'Create another chat', 'created',
                                        conversation_mode='new')
    initial = await approve(context, created)
    plan = initial['native_execution']
    hook(context.broker, context, plan)
    hook(context.broker, context, plan, name='PostToolUse',
         tool_response=receipt_value({'threadId': CHILD, 'hostId': 'local'}))
    await context.broker.send('codex', created['id'], 'answer', 'Created child answer',
                              'created-answer', initial['id'])
    await context.broker.finish('antigravity', created['id'])

    with pytest.raises(ConnectorError) as error:
        await context.broker.continue_channel('antigravity', created['id'],
                                              'Continue created child', 'round-2')

    assert error.value.code == 'codex_chat_reserved'
    assert context.broker.channel(created['id'])['status'] == 'active'
    assert context.broker.conversations.existing_route(selected['id']) == SELECTED


async def test_stale_selected_metadata_blocks_continuation_without_reopening(context):
    broker = context.broker
    task = await open_selected(broker)
    initial = await approve(context, task)
    await execute_send(context, task, initial)
    await broker.send('codex', task['id'], 'answer', 'Done', 'answer-1', initial['id'])
    await broker.finish('antigravity', task['id'])
    context.catalog.stale = True

    with pytest.raises(ConnectorError) as error:
        await broker.continue_channel('antigravity', task['id'], 'More work', 'round-2')

    assert error.value.code == 'stale_conversation_config'
    assert broker.channel(task['id'])['status'] == 'active'
    assert broker.db.execute('SELECT count(*) FROM messages WHERE channel=?',
                             (task['id'],)).fetchone()[0] == 2


async def test_selected_route_is_disabled_without_catalog_source(tmp_path):
    broker = Broker(tmp_path / 'disabled.sqlite3', conversations={'codex': REGISTRATION})
    broker.register('codex', 'codex-token')
    broker.register('antigravity', 'antigravity-token')
    try:
        with pytest.raises(ConnectorError) as error:
            await open_selected(broker)
        assert error.value.code == 'codex_binding_unavailable'
        assert broker.db.execute('SELECT count(*) FROM channels').fetchone()[0] == 0
    finally:
        broker.close()
