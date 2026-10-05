import asyncio
import json

import pytest
from mcp import Client
from mcp_types import ElicitResult

from test_participant_mcp import participants, parameters
from test_native_host import Host, REGISTRATION
from local_ai_connector.native_host import NativeHost


@pytest.mark.parametrize('mode', ['legacy', '2026-07-28'])
@pytest.mark.parametrize('decision', ['accept', 'decline', 'cancel'])
async def test_real_mcp_archive_has_separate_bound_approval(participants, mode, decision):
    app, paths = participants
    broker = app.state.broker
    broker.conversations.registrations['reviewer'] = REGISTRATION
    host = Host()
    native = NativeHost(broker, {'reviewer': host})
    task = await broker.open('writer', 'reviewer', 'New native chat', 'original', conversation_mode='new')
    await broker.decide(task['id'], True, host_approval=('writer', 'accept'))
    await native.step()
    question = (await broker.receive('reviewer', task['id'], timeout=0))['messages'][0]
    await broker.send('reviewer', task['id'], 'answer', 'Native answer', 'answer', question['id'])
    conversation = broker.conversations.describe(task['id'])['thread_id']
    prompts = []
    async def confirm(ctx, params):
        prompts.append(params.message)
        assert 'Archive conversation' in params.message
        assert conversation in params.message and task['id'] in params.message
        assert [op for op, _ in host.calls] == ['create']
        return ElicitResult(action=decision, content={} if decision == 'accept' else None)
    runner = asyncio.create_task(native.run())
    try:
        async with Client(parameters(paths[0]), mode=mode, elicitation_callback=confirm) as requester:
            args = {'channel': task['id'], 'conversation_id': conversation, 'request_key': 'archive', 'timeout_seconds': 3}
            result = await requester.call_tool('connector_archive', args)
            assert not result.is_error, result.content
            body = json.loads(result.content[0].text)
            assert len(prompts) == 1
            if decision == 'accept':
                assert body['state'] == 'completed' and body['conversation']['state'] == 'archived'
                assert body['next_round'] is None
                assert json.loads(body['answer']['body'])['registration_removed'] is True
                again = await requester.call_tool('connector_archive', args)
                assert not again.is_error and len(prompts) == 1
                blocked = await requester.call_tool('connector_continue', {'channel': task['id'], 'message': 'Wake again', 'request_key': 'replay'})
                assert blocked.is_error and 'conversation_archived' in blocked.content[0].text
            else:
                assert body['state'] == 'denied'
                assert [op for op, _ in host.calls] == ['create']
            rows = broker.db.execute('SELECT channel,decision FROM approval_decisions').fetchall()
            assert len(rows) == 2 and rows[0]['channel'] != rows[1]['channel']
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError): await runner
        await native.close()


async def test_archive_rejects_existing_chat_before_elicitation(participants):
    app, paths = participants
    broker = app.state.broker
    task = await broker.open('writer', 'reviewer', 'Existing task', 'existing')
    async def confirm(ctx, params):
        raise AssertionError('Existing chats cannot gain cleanup authority')
    async with Client(parameters(paths[0]), mode='legacy', elicitation_callback=confirm) as requester:
        result = await requester.call_tool('connector_archive', {'channel': task['id'], 'conversation_id': 'unowned', 'request_key': 'archive'})
        assert result.is_error and 'invalid_archive_target' in result.content[0].text
    assert broker.db.execute('SELECT count(*) FROM channels').fetchone()[0] == 1


async def test_antigravity_new_chat_and_followup_use_real_mcp_channel(participants):
    app, paths = participants
    broker = app.state.broker
    broker.conversations.registrations['reviewer'] = REGISTRATION
    host = Host(); native = NativeHost(broker, {'reviewer': host})
    runner = asyncio.create_task(native.run())
    prompts = []
    async def confirm(ctx, params):
        prompts.append(params.message)
        return ElicitResult(action='accept', content={})
    try:
        async with Client(parameters(paths[0]), mode='legacy', elicitation_callback=confirm) as requester, Client(parameters(paths[1]), mode='legacy') as worker:
            async def answer(expected_calls, text):
                async with asyncio.timeout(5):
                    while len(host.calls) < expected_calls:
                        await asyncio.sleep(.01)
                import re
                channel = re.search(r'connector channel ([a-f0-9-]+)', host.calls[-1][1]['text'])[1]
                response = await worker.call_tool('connector_receive', {'channel': channel, 'timeout': 3})
                message = next(m for m in json.loads(response.content[0].text)['messages'] if m['kind'] == 'question' and not m['resolved'])
                response = await worker.call_tool('connector_send', {'channel': channel, 'message': text, 'reply_to': message['id'], 'message_key': message['id']})
                assert not response.is_error
            answering = asyncio.create_task(answer(1, '7'))
            response = await requester.call_tool('connector_delegate', {'target': 'reviewer', 'message': 'Open a new chat: 3+4',
                    'request_key': 'new-native', 'conversation_mode': 'new', 'timeout_seconds': 5})
            assert not response.is_error, response.content
            first = json.loads(response.content[0].text)
            await answering
            assert first['state'] == 'completed' and first['answer']['body'] == '7'
            answering = asyncio.create_task(answer(2, '9'))
            response = await requester.call_tool('connector_continue', {'channel': first['channel'], 'message': 'Add 2', 'request_key': 'second', 'timeout_seconds': 5})
            assert not response.is_error, response.content
            second = json.loads(response.content[0].text)
            await answering
            assert second['answer']['body'] == '9' and second['conversation']['thread_id'] == first['conversation']['thread_id']
            assert len(prompts) == 1
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError): await runner
        await native.close()
