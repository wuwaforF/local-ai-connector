import json
import os
from pathlib import Path
import shlex
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from local_ai_connector.mcp_server import create_mcp


async def forbidden(*args, **kwargs):
    raise AssertionError('Reading usage must not execute broker, approval or binding callbacks')


@pytest.mark.parametrize('locale', ['zh-CN', 'en-US'])
@pytest.mark.parametrize('profile', ['peer', 'requester', 'participant', 'host_permission'])
async def test_terminal_guidance_survives_all_profile_instruction_selection(locale, profile):
    options = {'mcp_locale': locale}
    if profile != 'peer':
        options['decision'] = forbidden
    if profile in ('participant', 'host_permission'):
        options['combined'] = True
    if profile == 'host_permission':
        options['approval_transport'] = 'host_tool_permission'
    async with Client(create_mcp(forbidden, **options), mode='legacy') as client:
        assert "host's terminal tools" in client.instructions
        assert 'legitimate current chat address' in client.instructions
        assert 'not a Claude host denial' in client.instructions
        assert 'connector://claude-binding-guide' not in client.instructions
        assert not (await client.list_resources()).resources


@pytest.mark.parametrize('mode', ['legacy', 'auto'])
async def test_binding_usage_is_discoverable_and_readable_without_executing_callbacks(mode):
    guide = '# Terminal binding\nAuthorized instructions, not an execution tool.\n'
    server = create_mcp(forbidden, binding_catalog=forbidden, binding_inspect=forbidden,
                        binding_guide=guide)
    async with Client(server, mode=mode) as client:
        if mode == 'auto':
            assert client.session.protocol_version == '2026-07-28'
        resources = (await client.list_resources()).resources
        assert [str(resource.uri) for resource in resources] == ['connector://claude-binding-guide']
        result = await client.read_resource('connector://claude-binding-guide')
        assert result.contents[0].text == guide
        assert 'connector://claude-binding-guide' in client.instructions
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert 'claude_binding_usage' not in names
        assert 'connector_claude_binding_inspect' in names


def test_guide_without_local_binding_configuration_is_rejected():
    with pytest.raises(ValueError, match='guide requires configured binding inspection'):
        create_mcp(forbidden, binding_guide='guide')


async def test_real_stdio_guide_renders_trusted_paths_and_does_not_change_files(tmp_path):
    root = tmp_path / "Installation's `literal` $(literal)"
    root.mkdir()
    data = tmp_path / "Data's directory"
    data.mkdir()
    config = data / 'claude_code.json'
    config.write_text(json.dumps({'url': 'http://127.0.0.1:1', 'peer': 'claude_code',
        'token': 'local-test-only-token', 'client': 'generic', 'mcp_locale': 'en-US'}))
    config.chmod(0o600)
    before = {path.relative_to(tmp_path): path.read_bytes()
              for path in tmp_path.rglob('*') if path.is_file()}
    params = StdioServerParameters(command=sys.executable, args=[
        '-B', '-m', 'local_ai_connector.cli', 'mcp', '--config', str(config),
        '--claude-binding-root', str(root)],
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
             'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    async with Client(params, mode='legacy') as client:
        result = await client.read_resource('connector://claude-binding-guide')
        text = result.contents[0].text
        assert text.isascii()
        assert '@@INSTALLATION_ROOT@@' not in text and '@@DATA_DIRECTORY@@' not in text
        root_assignment = next(line for line in text.splitlines() if line.startswith('connector_root='))
        data_assignment = next(line for line in text.splitlines() if line.startswith('connector_data='))
        assert shlex.split(root_assignment.split('=', 1)[1]) == [str(root)]
        assert shlex.split(data_assignment.split('=', 1)[1]) == [str(data)]
        assert 'local-test-only-token' not in text
        assert 'register-claude-code.command' in text
        assert '--replace-binding' in text and '--capture-socket' in text
    assert {path.relative_to(tmp_path): path.read_bytes()
            for path in tmp_path.rglob('*') if path.is_file()} == before
