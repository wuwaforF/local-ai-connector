"""Human-selected session binding using actual output from that session's Bash tool."""
import json
import os
from pathlib import Path
import secrets
import shlex
import stat
import sys
import unicodedata
from uuid import UUID

from integrations.claude_code import onboarding


from local_ai_connector.claude_binding import strict_json, label, candidates

def same_workspace(actual, expected):
    try:
        return os.path.samefile(actual, expected)
    except FileNotFoundError as exc:
        raise ValueError('实际输出或所选聊天的工作区已不存在，请重新检查目标。') from exc


def receipt_prompt(nonce):
    code = ("import json,os; "
        "print(json.dumps({'schema':1,'nonce':" + repr(nonce) + ","
        "'cli_session_id':os.environ['CLAUDE_CODE_SESSION_ID'],"
        "'workspace':os.getcwd(),"
        "'messaging_socket':os.environ['CLAUDE_CODE_MESSAGING_SOCKET']}))")
    return ("Please run only this read-only Bash command in THIS selected Code chat and show its exact "
        "tool output. Do not reconstruct the JSON yourself. Stop afterward; do not call connector tools "
        "or change files, permissions, settings, or other chats. If the native permission system denies "
        "this action, stop and show its exact denial. Do not retry through another command or tool.\n\npython3 -c "
        + shlex.quote(code))


def validate_receipt(text, nonce, identity):
    if len(text) > 16384:
        raise ValueError('Receipt is too large')
    receipt = strict_json(text)
    fields = {'schema', 'nonce', 'cli_session_id', 'workspace', 'messaging_socket'}
    if (not isinstance(receipt, dict) or set(receipt) != fields or type(receipt['schema']) is not int
            or receipt['schema'] != 1 or receipt['nonce'] != nonce):
        raise ValueError('Receipt must be the exact JSON tool output from this wizard invocation')
    for field in fields - {'schema'}:
        if not isinstance(receipt[field], str) or not 0 < len(receipt[field]) <= 4096 or '\0' in receipt[field]:
            raise ValueError('Invalid receipt string: ' + field)
    if str(UUID(receipt['cli_session_id'])) != receipt['cli_session_id']:
        raise ValueError('Receipt session ID must be a canonical UUID')
    if not Path(receipt['workspace']).is_absolute():
        raise ValueError('Receipt workspace must be absolute')
    if (receipt['cli_session_id'] != identity['cliSessionId']
            or not same_workspace(receipt['workspace'], identity['cwd'])):
        raise ValueError('实际输出与所选聊天不一致。请回到正确聊天重新运行提示词；不会自动改选其他聊天。')
    if not Path(receipt['messaging_socket']).is_absolute():
        raise ValueError('Receipt inbox address must be absolute')
    return receipt


def project_mcp(root, data, workspace):
    path = workspace / '.mcp.json'
    if path.is_symlink():
        raise ValueError('Project MCP configuration must not be a symbolic link')
    if path.exists() and (not path.is_file() or path.stat().st_uid != os.getuid()):
        raise ValueError('Project MCP configuration must be an owner-controlled regular file')
    original = path.read_bytes() if path.exists() else None
    doc = strict_json(original) if original is not None else {}
    if not isinstance(doc, dict) or not isinstance(doc.get('mcpServers', {}), dict):
        raise ValueError('Invalid project MCP configuration')
    args = ['-m', 'local_ai_connector.cli', 'mcp', '--config', str(data / 'claude_code.json')]
    servers = doc.setdefault('mcpServers', {})
    existing = servers.get('local_ai_connector')
    if existing is not None:
        if (not isinstance(existing, dict) or existing.get('args') != args
                or not isinstance(existing.get('command'), str) or not Path(existing['command']).is_absolute()
                or not Path(existing['command']).is_file()):
            raise ValueError('项目已有不同的 local_ai_connector 配置。请先核对安装，不会覆盖它。')
        return path, original, original
    # A new project entry must point at the user's own installed runtime.
    python = root / '.venv/bin/python'
    if not python.is_file():
        raise ValueError('请先安装连接器 Python 运行环境；绑定向导不会复制其他用户的环境或凭据。')
    protected = Path.home() / 'Documents'
    if root == protected or protected in root.parents:
        raise ValueError('新项目请先将连接器运行环境安装在 Claude 可读目录，例如 ~/local-ai-connector。')
    servers['local_ai_connector'] = {'command': str(python), 'args': args}
    return path, original, (json.dumps(doc, ensure_ascii=False, indent=2) + '\n').encode()


def preview(root, data, selected, receipt):
    identity = selected['identity']
    metadata, desktop, cli = selected['metadata'], identity['sessionId'], identity['cliSessionId']
    workspace = Path(identity['cwd'])
    if not same_workspace(receipt['workspace'], workspace):
        raise ValueError('Selected workspace changed after receipt')
    if onboarding.inspect_identity(metadata, desktop, cli, workspace) != identity:
        raise ValueError('所选聊天信息已变化，请重新选择并获取输出。')
    record = data / (cli + '.json')
    captured = onboarding.capture(metadata, desktop, cli, workspace, record, Path(receipt['messaging_socket']))
    target = {'session_id': cli, 'workspace': str(workspace)}
    originals, replacements, report = onboarding.prepare_configuration(root, data, identity, target, record,
                                                                       create_binding=True)
    if report['wake_compatibility_issues']:
        raise ValueError('现有唤醒配置不兼容：' + '; '.join(report['wake_compatibility_issues']))
    path, original, proposed = project_mcp(root, data, workspace)
    originals[path] = original
    if proposed != original:
        replacements[path] = proposed
    previous = json.loads(originals[data / 'wakeup.json'])['bindings'].get('claude_code')
    return {'root': root, 'data': data, 'selected': selected, 'receipt': receipt, 'record': record,
            'capture': captured, 'originals': originals, 'replacements': replacements, 'report': report,
            'previous_target': previous['target'] if previous else None}


def commit(plan, plist):
    root, data, selected, receipt = (plan[k] for k in ('root', 'data', 'selected', 'receipt'))
    identity, metadata = selected['identity'], selected['metadata']
    desktop, cli, workspace = identity['sessionId'], identity['cliSessionId'], Path(identity['cwd'])
    if not same_workspace(receipt['workspace'], workspace):
        raise ValueError('Selected workspace changed after preview')
    record = plan['record']
    if onboarding.inspect_identity(metadata, desktop, cli, workspace) != identity:
        raise ValueError('Selected chat changed after confirmation')
    if any((p.read_bytes() if p.exists() else None) != v for p, v in plan['originals'].items()):
        raise ValueError('Configuration changed after confirmation; run the wizard again')
    current = onboarding.capture(metadata, desktop, cli, workspace, record, Path(receipt['messaging_socket']))
    if (current['socket_dev'], current['socket_ino']) != (plan['capture']['socket_dev'], plan['capture']['socket_ino']):
        raise ValueError('Chat inbox changed after confirmation; get a fresh tool receipt')
    onboarding.quiet(data)
    snapshot = {}
    onboarding.capture(metadata, desktop, cli, workspace, record, Path(receipt['messaging_socket']), write=True,
        expected_socket_identity=(plan['capture']['socket_dev'], plan['capture']['socket_ino']),
        rollback_receipt=snapshot)
    mcp_path = workspace / '.mcp.json'
    expected_mcp = plan['replacements'].get(mcp_path, plan['originals'][mcp_path])
    def mcp_ready():
        if mcp_path.is_symlink() or mcp_path.read_bytes() != expected_mcp:
            raise RuntimeError('Project MCP configuration changed during binding')
        record_ready()
    def record_ready():
        if record.is_symlink() or record.read_bytes() != snapshot['written']:
            raise RuntimeError('Session record changed during binding')
    try:
        record_ready()
        onboarding.install(root, data, plan['originals'], plan['replacements'], metadata, desktop, cli,
            workspace, record, plist, replace_binding=plan['report']['previous_binding_sha256'], create_binding=True,
            extra_readiness=mcp_ready, extra_validation=record_ready)
    except BaseException:
        # Restore only our own capture; a concurrent writer is never overwritten.
        if record.is_symlink() or (record.read_bytes() if record.exists() else None) != snapshot['written']:
            raise RuntimeError('Binding failed and capture changed concurrently; preserve it for recovery')
        if snapshot['previous'] is None:
            record.unlink()
        else:
            onboarding.replace(record, snapshot['previous'])
        raise


def run(*, root=onboarding.ROOT, home=None, read=input, write=print):
    home = Path.home() if home is None else home
    data = home / '.local/share/local-ai-connector'
    required = ('server.json', 'wakeup.json', 'claude_code.json', 'state.sqlite3')
    missing = [name for name in required if not (data / name).is_file()]
    if missing:
        raise ValueError('需先完成一次连接器服务安装和 Claude 端点登记。缺少：' + ', '.join(missing)
                         + '。参见 docs/CLAUDE_ONBOARDING.md；当前尚未开始绑定。')
    write('Claude 会话绑定向导（预览）：请选择已打开的本地 Code 聊天。')
    unavailable = []
    items = candidates(home / 'Library/Application Support/Claude/claude-code-sessions', unavailable=unavailable)
    for reason in unavailable:
        write('不可绑定：' + reason)
    if not items:
        raise ValueError('没有可绑定的本地 Code 聊天。请先创建聊天并完成原生工作区信任。')
    for i, item in enumerate(items, 1):
        identity = item['identity']
        write(f"{i}. {item['title']} | {label(identity['cwd'])} | {identity['sessionId']}")
    choice = read('输入目标聊天编号（回车取消）：').strip()
    if not choice:
        write('已取消。')
        return
    if not choice.isdecimal() or not 1 <= int(choice) <= len(items):
        raise ValueError('请选择列表中的一个编号')
    selected = items[int(choice) - 1]
    nonce = secrets.token_hex(16)
    write('将以下提示词复制到这个 Claude 聊天中，运行后只复制实际工具输出的 JSON：')
    write('若 Claude 拒绝或告警，请停止并回车取消向导；此处确认不能替代 Claude 的原生审批。')
    write(receipt_prompt(nonce))
    text = read('粘贴 JSON 工具输出（单行，回车取消）：').strip()
    if not text:
        write('已取消。')
        return
    receipt = validate_receipt(text, nonce, selected['identity'])
    plan = preview(root, data, selected, receipt)
    write('目标聊天：' + selected['title'])
    write('会话标识：' + selected['identity']['sessionId'])
    write('工作区：' + label(selected['identity']['cwd']))
    write('原绑定：' + label(plan['previous_target'] or '无；本次创建首次绑定'))
    write('将更新：' + ', '.join(str(p) for p in [plan['record'], *plan['replacements']]))
    write('绑定变更时会备份配置并重启本连接器服务；原生信任及任务审批仍在 Claude/Codex 中处理。')
    if read('确认绑定此聊天？[y/N]：').strip().lower() not in ('y', 'yes'):
        write('已取消；绑定和配置未修改。')
        return
    commit(plan, home / 'Library/LaunchAgents/dev.local-ai-connector.service.plist')
    write('绑定已保存。请在 Claude 中确认项目 MCP；从发起端批准一次测试，才能确认真实往返可用。')


if __name__ == '__main__':
    try:
        run()
    except (ValueError, OSError) as exc:
        print('绑定未完成：' + str(exc), file=sys.stderr)
        raise SystemExit(1)
