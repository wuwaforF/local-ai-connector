import asyncio
import json

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult, InputRequiredResult

from local_ai_connector.conversations import handle_hook
from local_ai_connector.mcp_server import create_mcp
from test_participant_mcp import participants, parameters
from test_codex_selected_routes import FakeCatalog, SELECTED, receipt
from test_conversations import REGISTRATION, SESSION, CHILD, event


async def forbidden(*args, **kwargs):
    raise AssertionError('Default discovery must not invoke a binding callback')


async def test_quick_binding_tools_are_not_advertised_by_default():
    async with Client(create_mcp(forbidden, decision=forbidden), mode='auto') as client:
        assert not any('codex_binding' in t.name or 'codex_quick' in t.name
                       for t in (await client.list_tools()).tools)
        assert not {'connector_codex_bind_chat', 'connector_codex_bound_chat'} & {
            t.name for t in (await client.list_tools()).tools}
        assert not (await client.list_resources()).resources


@pytest.mark.parametrize('mode', ['legacy', 'auto'])
@pytest.mark.parametrize('profile', ['peer', 'requester', 'participant', 'host_permission'])
async def test_opt_in_guide_is_readable_without_binding_or_approval(mode, profile):
    options = {'codex_quick_binding': True, 'mcp_locale': 'en-US'}
    if profile != 'peer':
        options['decision'] = forbidden
    if profile in ('participant', 'host_permission'):
        options['combined'] = True
    if profile == 'host_permission':
        options['approval_transport'] = 'host_tool_permission'
    async with Client(create_mcp(forbidden, **options), mode=mode) as client:
        resources = (await client.list_resources()).resources
        assert [str(r.uri) for r in resources] == ['connector://codex-binding-guide']
        guide = (await client.read_resource('connector://codex-binding-guide')).contents[0].text
        assert guide.isascii()
        assert 'thread/read(includeTurns=false)' in guide
        assert 'Native receipt must return the approved' in guide
        assert '47 * 19 = 893' in guide and '+7 = 900' in guide
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert 'connector_codex_binding_catalog' in tools
        assert 'connector_codex_bound_chat' in tools
        assert ('connector_codex_bind_chat' in tools) == (profile != 'peer')
        assert ('connector_codex_quick_bind' in tools) == (profile != 'peer')


@pytest.mark.parametrize('mode', ['legacy', 'auto'])
async def test_host_permission_selected_result_preserves_target_and_exact_task(participants, mode):
    app, paths = participants
    broker = app.state.broker
    broker.codex_catalog = FakeCatalog()
    broker.conversations.registrations['reviewer'] = REGISTRATION
    config = json.loads(paths[0].read_text())
    config['approval_transport'] = 'host_tool_permission'
    paths[0].write_text(json.dumps(config))
    params = parameters(paths[0])
    params.args.append('--codex-quick-binding')
    # The client simulates the configured host gate; this proves no human consent.
    async with Client(params, mode=mode) as requester:
        tools = {t.name: t for t in (await requester.list_tools()).tools}
        assert tools['connector_codex_quick_bind'].meta['anthropic/requiresUserInteraction'] is True
        assert not (tools['connector_codex_binding_catalog'].meta or {}).get('anthropic/requiresUserInteraction')
        assert not (tools['connector_continue'].meta or {}).get('anthropic/requiresUserInteraction')
        args = {'target': 'reviewer', 'selected_conversation': SELECTED,
                'message': '47 * 19', 'request_key': 'selected-host-gate'}
        result = await requester.call_tool('connector_codex_quick_bind', args)
        assert not result.is_error, result.content
        task = json.loads(result.content[0].text)
        assert task['state'] == 'active' and task['next_tool'] == 'connector_receive'
        assert task['conversation']['selected'] == SELECTED
        assert task['conversation']['thread_id'] == CHILD
        assert task['conversation']['created'] is False
        changed = await requester.call_tool('connector_codex_quick_bind', {
            **args, 'selected_conversation': {**SELECTED, 'thread_id': SESSION}})
        assert changed.is_error
        question = (await broker.receive('reviewer', task['channel'], timeout=0))['messages'][0]
        assert question['body'] == args['message']
        assert question['native_execution']['arguments'] == {
            'threadId': CHILD, 'hostId': 'local', 'prompt': args['message']}
        assert [dict(r) for r in broker.db.execute('SELECT source,decision FROM approval_decisions')] == [
            {'source': 'host_tool_permission', 'decision': 'accept'}]


@pytest.mark.parametrize('mode', ['legacy', 'auto'])
@pytest.mark.parametrize('transport,decision', [('elicitation', 'accept'), ('elicitation', 'decline'),
    ('elicitation', 'cancel'), ('host_tool_permission', 'accept')])
async def test_initiating_host_saves_target_without_task_or_native_execution(participants, mode, transport, decision):
    app, paths = participants
    broker = app.state.broker
    broker.codex_catalog = FakeCatalog()
    broker.conversations.registrations['reviewer'] = REGISTRATION
    config = json.loads(paths[0].read_text())
    config['approval_transport'] = transport
    paths[0].write_text(json.dumps(config))
    prompts = []
    async def confirmation(ctx, params):
        assert transport == 'elicitation'
        prompts.append(params.message)
        assert all(value in params.message for value in (SELECTED['title'], CHILD, '/target', 'writer', 'reviewer'))
        assert 'sends no task' in params.message and 'Each new task still requires initiating-host approval' in params.message
        assert 'Authorization expires one hour' not in params.message
        pending = broker.snapshot()['channels'][0]
        assert pending['conversation']['binding_only'] is True
        assert pending['conversation']['selected'] == SELECTED
        return ElicitResult(action=decision, content={} if decision == 'accept' else None)
    params = parameters(paths[0])
    params.args.append('--codex-quick-binding')
    # Direct tools/call simulates the mandatory native gate for that transport.
    async with Client(params, mode=mode, elicitation_callback=confirmation) as requester:
        tools = {t.name: t for t in (await requester.list_tools()).tools}
        assert bool((tools['connector_codex_bind_chat'].meta or {}).get('anthropic/requiresUserInteraction')) == (transport == 'host_tool_permission')
        assert not (tools['connector_codex_bound_chat'].meta or {}).get('anthropic/requiresUserInteraction')
        catalog = json.loads((await requester.call_tool('connector_codex_binding_catalog', {'target': 'reviewer'})).content[0].text)
        assert catalog['binding_usage']['approval_transport'] == transport
        args = {'target': 'reviewer', 'selected_conversation': SELECTED, 'request_key': 'save-selected'}
        result = await requester.call_tool('connector_codex_bind_chat', args)
        assert not result.is_error, result.content
        saved = json.loads(result.content[0].text)
        assert saved['state'] == ('bound' if decision == 'accept' else 'denied')
        assert len(prompts) == (1 if transport == 'elicitation' else 0)
        assert broker.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
        assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0
        audit = [dict(r) for r in broker.db.execute('SELECT source,peer,decision FROM approval_decisions')]
        assert audit == [{'source': 'host_elicitation' if transport == 'elicitation' else transport,
                          'peer': 'writer', 'decision': decision}]
        if decision != 'accept':
            assert broker.conversations.saved_binding('writer', 'reviewer') is None
            return
        assert saved['task_authorized'] is False and saved['conversation']['selected'] == SELECTED
        replayed = await requester.call_tool('connector_codex_bind_chat', args)
        assert json.loads(replayed.content[0].text) == saved
        assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 1
        resolved = await requester.call_tool('connector_codex_bound_chat', {'target': 'reviewer'})
        assert not resolved.is_error, resolved.content
        ready = json.loads(resolved.content[0].text)
        assert ready['selected_conversation'] == SELECTED and ready['task_authorized'] is False
        forbidden_task = await requester.call_tool('connector_continue', {'channel': saved['channel'],
            'message': 'Send without task approval', 'request_key': 'forbidden', 'timeout_seconds': 1})
        assert forbidden_task.is_error and 'binding_channel' in forbidden_task.content[0].text


@pytest.mark.parametrize('mutation', ['selection', 'task'])
async def test_modern_confirmation_cannot_authorize_changed_selected_task(participants, mutation):
    app, paths = participants
    broker = app.state.broker
    broker.codex_catalog = FakeCatalog()
    broker.conversations.registrations['reviewer'] = REGISTRATION
    params = parameters(paths[0])
    params.args.append('--codex-quick-binding')
    async with Client(params, mode='auto', elicitation_callback=forbidden) as requester:
        args = {'target': 'reviewer', 'selected_conversation': SELECTED,
                'message': '47 * 19', 'request_key': 'selected-bound-confirmation', 'timeout_seconds': 60}
        first = await requester.session.call_tool('connector_codex_quick_bind', args, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        request_id = next(iter(first.input_requests))
        changed = {**args}
        if mutation == 'selection':
            changed['selected_conversation'] = {**SELECTED, 'title': 'A different selection'}
        else:
            changed['message'] = 'A substituted task'
        with pytest.raises(MCPError, match='Invalid or expired requestState'):
            await requester.session.call_tool('connector_codex_quick_bind', changed,
                input_responses={request_id: ElicitResult(action='accept', content={})},
                request_state=first.request_state, allow_input_required=True)
        assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 0
        assert broker.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
        assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0
        assert [json.loads(r[0]) for r in broker.db.execute('SELECT selection FROM native_existing_routes')] == [SELECTED]
        declined = await requester.session.call_tool('connector_codex_quick_bind', args,
            input_responses={request_id: ElicitResult(action='decline')},
            request_state=first.request_state, allow_input_required=True)
        assert not declined.is_error and json.loads(declined.content[0].text)['state'] == 'denied'


@pytest.mark.parametrize('mode', ['legacy', 'auto'])
@pytest.mark.parametrize('decision', ['accept', 'decline', 'cancel'])
async def test_exact_selected_chat_has_one_initiating_confirmation(participants, mode, decision):
    app, paths = participants
    broker = app.state.broker
    catalog = broker.codex_catalog = FakeCatalog()
    broker.conversations.registrations['reviewer'] = REGISTRATION
    prompts = []
    async def confirmation(ctx, params):
        prompts.append(params.message)
        assert all(value in params.message for value in ('47 * 19', SELECTED['title'], CHILD, '/target'))
        assert broker.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
        assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0
        return ElicitResult(action=decision, content={} if decision == 'accept' else None)
    request_params = parameters(paths[0])
    request_params.args.append('--codex-quick-binding')
    async with Client(request_params, mode=mode, elicitation_callback=confirmation) as requester, \
            Client(parameters(paths[1]), mode=mode) as worker:
        listed = await requester.call_tool('connector_codex_binding_catalog', {'target': 'reviewer'})
        assert not listed.is_error
        selected = json.loads(listed.content[0].text)['choices'][0]
        cursor = 0

        async def synthetic_worker_answer(body):
            nonlocal cursor
            incoming = await worker.call_tool('connector_receive', {'after': cursor, 'timeout': 60})
            received = json.loads(incoming.content[0].text)
            cursor = received['cursor']
            question = next(q for q in received['messages'] if q['kind'] == 'question' and not q['resolved'])
            plan = question['native_execution']
            assert plan['arguments']['threadId'] == CHILD and plan['arguments']['hostId'] == 'local'
            handle_hook(broker.db, event(plan), 'reviewer', SESSION, '/worker', broker.clock(),
                        selected_verifier=catalog.verify_hook)
            handle_hook(broker.db, event(plan, 'PostToolUse', tool_response=receipt()),
                        'reviewer', SESSION, '/worker', broker.clock())
            sent = await worker.call_tool('connector_send', {'channel': question['channel'], 'message': body,
                'reply_to': question['id'], 'message_key': question['id']})
            assert not sent.is_error
        answer = asyncio.create_task(synthetic_worker_answer('893')) if decision == 'accept' else None
        args = {'target': 'reviewer', 'selected_conversation': selected, 'message': '47 * 19',
                'request_key': 'selected-first', 'timeout_seconds': 60}
        first = await requester.call_tool('connector_codex_quick_bind', args)
        assert not first.is_error, first.content
        result = json.loads(first.content[0].text)
        assert len(prompts) == 1
        if answer is None:
            assert result['state'] == 'denied'
            assert broker.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
            assert broker.db.execute('SELECT count(*) FROM native_operations').fetchone()[0] == 0
            return
        await answer
        assert result['answer']['body'] == '893' and result['conversation']['created'] is False
        assert result['conversation']['thread_id'] == CHILD
        repeated = await requester.call_tool('connector_codex_quick_bind', args)
        assert json.loads(repeated.content[0].text) == result and len(prompts) == 1
        answer = asyncio.create_task(synthetic_worker_answer('900'))
        continued = await requester.call_tool('connector_continue', {'channel': result['channel'],
            'message': 'Add 7', 'request_key': 'selected-second', 'timeout_seconds': 60})
        assert not continued.is_error
        await answer
        second = json.loads(continued.content[0].text)
        assert second['channel'] == result['channel'] and second['answer']['body'] == '900'
        assert second['conversation']['thread_id'] == CHILD
        assert broker.db.execute('SELECT count(*) FROM approval_decisions').fetchone()[0] == 1


async def test_requester_can_answer_first_task_clarification_with_same_selection(participants):
    app, paths = participants
    broker = app.state.broker
    catalog = broker.codex_catalog = FakeCatalog()
    broker.conversations.registrations['reviewer'] = REGISTRATION
    config = json.loads(paths[0].read_text())
    config['tool_profile'] = 'requester'
    paths[0].write_text(json.dumps(config))
    prompts = []
    async def confirmation(ctx, params):
        prompts.append(params.message)
        return ElicitResult(action='accept', content={})
    params = parameters(paths[0])
    params.args.append('--codex-quick-binding')
    async with Client(params, mode='auto', elicitation_callback=confirmation) as requester, \
            Client(parameters(paths[1]), mode='auto') as worker:
        assert 'connector_send' not in {t.name for t in (await requester.list_tools()).tools}
        args = {'target': 'reviewer', 'selected_conversation': SELECTED, 'message': 'Calculate 893 plus an increment',
                'request_key': 'clarified-selected', 'timeout_seconds': 60}
        async def ask():
            incoming = await worker.call_tool('connector_receive', {'timeout': 60})
            q = json.loads(incoming.content[0].text)['messages'][0]
            plan = q['native_execution']
            handle_hook(broker.db, event(plan), 'reviewer', SESSION, '/worker', broker.clock(),
                        selected_verifier=catalog.verify_hook)
            handle_hook(broker.db, event(plan, 'PostToolUse', tool_response=receipt()),
                        'reviewer', SESSION, '/worker', broker.clock())
            result = await worker.call_tool('connector_send', {'channel': q['channel'], 'kind': 'question',
                'message': 'What increment?', 'message_key': 'clarification', 'reply_to': q['id']})
            return q, json.loads(result.content[0].text)
        asking = asyncio.create_task(ask())
        result = await requester.call_tool('connector_codex_quick_bind', args)
        assert not result.is_error
        pending = json.loads(result.content[0].text)
        assert pending['state'] == 'input_required'
        initial, clarification = await asking
        async def answer():
            incoming = await worker.call_tool('connector_receive', {'channel': initial['channel'],
                'after': clarification['seq'], 'timeout': 60})
            msg = next(q for q in json.loads(incoming.content[0].text)['messages'] if q['kind'] == 'answer')
            plan = msg['native_execution']
            assert plan['arguments'] == {'threadId': CHILD, 'hostId': 'local', 'prompt': '7'}
            handle_hook(broker.db, event(plan), 'reviewer', SESSION, '/worker', broker.clock(),
                        selected_verifier=catalog.verify_hook)
            handle_hook(broker.db, event(plan, 'PostToolUse', tool_response=receipt()),
                        'reviewer', SESSION, '/worker', broker.clock())
            result = await worker.call_tool('connector_send', {'channel': initial['channel'],
                'message': '900', 'message_key': 'clarified-final', 'reply_to': initial['id']})
            assert not result.is_error
        answering = asyncio.create_task(answer())
        result = await requester.call_tool('connector_codex_quick_bind', {
            **args, 'reply_to': clarification['id'], 'reply': '7'})
        assert not result.is_error
        await answering
        assert json.loads(result.content[0].text)['answer']['body'] == '900'
        assert len(prompts) == 1
