import importlib.util
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

spec = importlib.util.spec_from_file_location('antigravity_native_bridge', Path(__file__).resolve().parents[1] / 'integrations/antigravity/native_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
PROJECT, CONVERSATION = str(uuid4()), str(uuid4())
TARGET = {'project_id': PROJECT, 'workspace_uri': 'file:///test', 'conversation_id': CONVERSATION}
ENV = {'ANTIGRAVITY_PROJECT_ID': PROJECT}


class RPC:
    def __init__(self):
        self.calls = []
        self.archived = False
        self.status = 'CASCADE_RUN_STATUS_IDLE'
        self.fail_archive_readback = False
        self.wrong_project = False
    def __call__(self, method, body, env):
        self.calls.append((method, body))
        assert env == ENV
        if method == 'GetConversationMetadata':
            return {'metadata': {'rootConversationId': CONVERSATION, 'projectId': 'wrong' if self.wrong_project else PROJECT,
                                 'workspaceUris': ['file:///test']}}
        if method == 'GetAllCascadeTrajectories':
            if self.fail_archive_readback and self.archived: raise TimeoutError('readback failed')
            return {'trajectorySummaries': {CONVERSATION: {'annotations': {'archived': self.archived, 'title': 'preserve'}, 'status': self.status}}}
        assert method == 'UpdateConversationAnnotations'
        assert body == {'cascadeIds': [CONVERSATION], 'annotations': {'archived': True}, 'mergeAnnotations': True}
        self.archived = True
        return {}


def test_archive_reads_back_and_preserves_annotations():
    rpc = RPC()
    result = bridge.handle({'op': 'archive', 'target': TARGET, 'expires_at': 200}, rpc_call=rpc, env=ENV, clock=lambda: 100)
    assert result == {'ok': True, 'archived': True, 'target': TARGET}
    assert [method for method, _ in rpc.calls] == ['GetConversationMetadata', 'GetAllCascadeTrajectories', 'UpdateConversationAnnotations', 'GetAllCascadeTrajectories']
    again = bridge.handle({'op': 'archive', 'target': TARGET, 'expires_at': 200}, rpc_call=rpc, env=ENV, clock=lambda: 100)
    assert again == result
    assert sum(method == 'UpdateConversationAnnotations' for method, _ in rpc.calls) == 1


@pytest.mark.parametrize('condition,error', [('expired', 'rejected'), ('busy', 'rejected'), ('wrong_project', 'stale_target'), ('readback', 'ambiguous')])
def test_archive_boundaries(condition, error):
    rpc = RPC()
    if condition == 'busy': rpc.status = 'CASCADE_RUN_STATUS_RUNNING'
    if condition == 'wrong_project': rpc.wrong_project = True
    if condition == 'readback': rpc.fail_archive_readback = True
    result = bridge.handle({'op': 'archive', 'target': TARGET, 'expires_at': 99 if condition == 'expired' else 200},
                           rpc_call=rpc, env=ENV, clock=lambda: 100)
    assert result['ok'] is False and result['error'] == error
    assert sum(method == 'UpdateConversationAnnotations' for method, _ in rpc.calls) == (1 if condition == 'readback' else 0)


def test_creation_uses_official_command_and_confirms_real_target():
    rpc = RPC()
    dispatch = str(uuid4())
    args = {'op': 'create', 'target': {k: TARGET[k] for k in ('project_id', 'workspace_uri')},
            'title': 'new chat', 'text': 'wake ' + dispatch, 'dispatch_id': dispatch, 'expires_at': 200}
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({'response': {'newConversation': {'conversationId': CONVERSATION}}}), '')
    result = bridge.handle(args, run=run, rpc_call=rpc, env=ENV, clock=lambda: 100)
    assert result == {'ok': True, 'target': TARGET}
    assert commands == [['agentapi', 'new-conversation', '--title=new chat', 'wake ' + dispatch]]


def test_creation_timeout_never_reports_definite_non_creation():
    dispatch = str(uuid4())
    def run(*args, **kwargs): raise subprocess.TimeoutExpired('agentapi', 20)
    result = bridge.handle({'op': 'create', 'target': {k: TARGET[k] for k in ('project_id', 'workspace_uri')},
        'title': 'test', 'text': dispatch, 'dispatch_id': dispatch, 'expires_at': 200}, run=run, env=ENV, clock=lambda: 100)
    assert result['error'] == 'ambiguous'


def test_archived_chat_cannot_be_woken():
    rpc = RPC(); rpc.archived = True
    result = bridge.handle({'op': 'native_send', 'target': TARGET, 'expires_at': 200}, rpc_call=rpc, env=ENV, clock=lambda: 100)
    assert result['error'] == 'rejected'
