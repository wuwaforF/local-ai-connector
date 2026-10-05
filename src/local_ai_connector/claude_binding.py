"""Read-only selected Claude session contracts shared by native MCP and bridge callers."""
import hashlib
import json
import os
from pathlib import Path
import stat
import unicodedata
from uuid import UUID

TARGET_FIELDS = {"session_id", "workspace"}

def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON fields are not accepted')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs)


def label(value):
    return ''.join(c if not unicodedata.category(c).startswith('C') else ' ' for c in str(value))[:240]


def session_identity(record, desktop_id, cli_id, workspace):
    if not isinstance(record, dict):
        raise ValueError('Expected a session metadata object')
    if str(UUID(cli_id)) != cli_id or not desktop_id:
        raise ValueError('Expected explicit Desktop identity and CLI UUID')
    if (record.get('sessionId') != desktop_id or record.get('cliSessionId') != cli_id
            or record.get('cwd') != str(workspace)):
        raise ValueError('Session identities or workspace do not match')
    if not workspace.is_absolute():
        raise ValueError('Workspace must be absolute')
    if 'originCwd' in record and not isinstance(record['originCwd'], str):
        raise ValueError('Invalid origin workspace metadata')
    if 'isArchived' in record and type(record['isArchived']) is not bool:
        raise ValueError('Invalid archived metadata')
    return {key: record[key] for key in ('sessionId', 'cliSessionId', 'cwd', 'originCwd', 'isArchived') if key in record}


def _metadata_identity(metadata, document, desktop_id, cli_id, workspace):
    if not metadata.is_absolute() or metadata.name != desktop_id + '.json':
        raise ValueError('Use the exact Desktop metadata file, not an inferred CLI filename')
    identity = session_identity(document, desktop_id, cli_id, workspace)
    if identity.get('isArchived') is not False:
        raise ValueError('The selected Claude chat must be explicitly unarchived')
    if identity.get('originCwd') != str(workspace):
        raise ValueError('This onboarding requires cwd and originCwd to match; worktree sessions need separate onboarding')
    return identity


def inspect_identity(metadata, desktop_id, cli_id, workspace):
    if metadata.is_symlink():
        raise ValueError('Use the exact Desktop metadata file, not a symbolic link')
    return _metadata_identity(metadata, json.loads(metadata.read_bytes()), desktop_id, cli_id, workspace)


def _read_candidate(path, unavailable=None):
    if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)):
        raise ValueError('Claude metadata path contains a symbolic link')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as file:
        info = os.fstat(file.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError('Claude metadata must be a bounded owner-controlled file')
        text = file.read(1024 * 1024 + 1)
    if len(text) > 1024 * 1024:
        raise ValueError('Claude metadata exceeds its size limit')
    obj = strict_json(text)
    if not isinstance(obj, dict):
        raise ValueError('Claude metadata must be an object')
    if obj.get('isArchived') is not False:
        return None
    desktop, cli, cwd = obj.get('sessionId'), obj.get('cliSessionId'), obj.get('cwd')
    title = obj.get('title') or obj.get('sessionTitle') or obj.get('name') or desktop
    if not all(isinstance(v, str) and v for v in (desktop, cli, cwd)):
        if unavailable is not None:
            unavailable.append(label(title) + '：本地会话引擎尚未就绪')
        return None
    if obj.get('originCwd') != cwd:
        if unavailable is not None:
            unavailable.append(label(title) + '：当前不支持此 worktree/工作区状态')
        return None
    identity = _metadata_identity(path, obj, desktop, cli, Path(cwd))
    return {'metadata': path, 'identity': identity, 'title': label(title),
            'source_identity': [info.st_dev, info.st_ino]}


def candidates(directory, *, unavailable=None):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('未找到 Claude 本地 Code 会话。请先在 Claude Desktop 的 Code 标签创建聊天。')
    found, ids = [], set()
    # Only the observed shallow Desktop layout; never inspect transcripts or inbox directories.
    for path in sorted(directory.glob('*/*/local_*.json')):
        choice = _read_candidate(path, unavailable)
        if choice is None:
            continue
        desktop = choice['identity']['sessionId']
        if desktop in ids:
            raise ValueError('Ambiguous Desktop identity; no chat was selected')
        ids.add(desktop)
        found.append(choice)
    return found


class StaleTarget(ValueError):
    pass


def socket_identity(socket_path):
    info = os.stat(socket_path, follow_symlinks=False)
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise StaleTarget("The inbox must be an owner-only Unix socket")
    return info


def confirmed_session(target, session_record):
    """Resolve the stable session identity to its current verified inbox snapshot."""
    if not isinstance(target, dict) or set(target) != TARGET_FIELDS:
        raise StaleTarget("An explicit session and workspace are required")
    if any(not isinstance(value, str) or not value for value in target.values()):
        raise StaleTarget("Target fields must be nonempty strings")
    try:
        if str(UUID(target["session_id"])) != target["session_id"]:
            raise ValueError
    except ValueError:
        raise StaleTarget("The pinned session identity is invalid") from None
    if not Path(target["workspace"]).is_absolute() or "\0" in target["workspace"]:
        raise StaleTarget("The workspace path must be absolute")
    session_record = Path(session_record)
    if not session_record.is_absolute() or session_record.name != f"{target['session_id']}.json":
        raise StaleTarget("An absolute record path naming the pinned session UUID is required")
    try:
        # O_NOFOLLOW and fstat validate the same opened file, including during replacement.
        with os.fdopen(os.open(session_record, os.O_RDONLY | os.O_NOFOLLOW), "r") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise StaleTarget("The session record must be an owner-only regular file")
            record = json.load(file)
        if not isinstance(record, dict):
            raise StaleTarget("The session record must be an object")
        for field in TARGET_FIELDS:
            if record.get(field) != target[field]:
                raise StaleTarget("The session record does not match the pinned target")
        socket_path = record.get("socket")
        if (not isinstance(socket_path, str) or not Path(socket_path).is_absolute()
                or "\0" in socket_path):
            raise StaleTarget("The session record must name an absolute inbox socket path")
        for field in ("socket_dev", "socket_ino"):
            if type(record.get(field)) is not int or record[field] < (1 if field == "socket_ino" else 0):
                raise StaleTarget("The session record must contain a valid socket identity")
        info = socket_identity(socket_path)
        if (info.st_dev != record["socket_dev"] or info.st_ino != record["socket_ino"]):
            raise StaleTarget("The inbox socket does not match the recorded owner, mode, and identity")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StaleTarget(f"The pinned session could not be verified: {type(exc).__name__}") from None
    return {"session_id": record["session_id"], "workspace": record["workspace"],
            "socket_path": socket_path, "socket_dev": info.st_dev, "socket_ino": info.st_ino}


def compatibility_issues(bindings, options):
    issues = []
    if options is None:
        issues.append('Wakeups must be explicitly enabled')
    elif options.get('send_when_unknown') is not True:
        issues.append('Claude inbox status is unknown; send_when_unknown must be true')
    if bindings['claude_code'].restore_enabled:
        issues.append('The Claude inbox bridge does not support restore')
    return issues


def _configuration(data):
    for name in ('server.json', 'wakeup.json', 'claude_code.json', 'state.sqlite3'):
        path = data / name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()):
            raise ValueError('连接器配置须为当前用户的普通文件。')
    server, wake, endpoint = (strict_json((data / name).read_bytes())
                            for name in ('server.json', 'wakeup.json', 'claude_code.json'))
    if (not all(isinstance(value, dict) for value in (server, wake, endpoint))
            or not isinstance(wake.get('bindings'), dict)
            or not isinstance(server.get('peers'), dict)
            or endpoint.get('peer') != 'claude_code' or endpoint.get('client') != 'generic'
            or not isinstance(endpoint.get('token'), str) or not endpoint['token']
            or endpoint['token'] != server['peers'].get('claude_code')
            or endpoint.get('url') != server.get('url')):
        raise ValueError('Claude 端点和服务配置不匹配，请先核对安装。')
    if endpoint.get('tool_profile') == 'participant' and (
            not endpoint.get('approval_token')
            or endpoint['approval_token'] != server.get('approval_tokens', {}).get('claude_code')):
        raise ValueError('Claude 审批端点和服务配置不匹配。')
    binding = wake['bindings'].get('claude_code')
    if binding is not None and not isinstance(binding, dict):
        raise ValueError('现有 Claude 绑定格式无效。')
    return server, wake, binding


def _choices(home):
    unavailable = []
    choices = candidates(home / 'Library/Application Support/Claude/claude-code-sessions',
                         unavailable=unavailable)
    return choices, unavailable


def _revision(choice, binding):
    from .conversations import canonical
    snapshot = {'identity': choice['identity'], 'title': choice['title'],
                'metadata': [str(choice['metadata']), *choice['source_identity']],
                'binding': binding}
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest()


def catalog(data, home):
    _, _, binding = _configuration(data)
    choices, unavailable = _choices(home)
    target = binding.get('target', {}) if binding else {}
    current = 'No Claude session binding'
    if target:
        current = 'An existing binding is configured; select its chat for local verification'
        for choice in choices:
            identity = choice['identity']
            if target == {'session_id': identity['cliSessionId'], 'workspace': identity['cwd']}:
                current = choice['title'] + ' · ' + label(identity['cwd'])
                break
    return {'schema_version': 1, 'status': 'catalog_ready', 'current': current,
            'message': 'Choose a chat to inspect. Row numbers are display labels only; this operation verifies local state.',
            'unavailable': unavailable, 'binding_commit_supported': False,
            'choices': [{'desktop_id': choice['identity']['sessionId'], 'title': choice['title'],
                         'workspace': label(choice['identity']['cwd']),
                         'selection_revision': _revision(choice, binding)} for choice in choices]}


def inspect(root, data, home, desktop_id, expected_revision):
    if (not isinstance(desktop_id, str) or not desktop_id or len(desktop_id) > 200
            or not isinstance(expected_revision, str) or len(expected_revision) != 64
            or any(c not in '0123456789abcdef' for c in expected_revision)):
        raise ValueError('请使用最新列表中所选会话的标识和版本。')
    server, wake, binding = _configuration(data)
    base = {'schema_version': 1, 'changes': [], 'binding_commit_supported': False}
    stale = {**base, 'status': 'catalog_stale',
             'message': 'The selected chat or binding changed. Refresh the catalog and obtain a new user selection.'}
    try:
        choices, _ = _choices(home)
        selected = next((choice for choice in choices if choice['identity']['sessionId'] == desktop_id), None)
        if selected is None:
            return stale
        selected = _read_candidate(selected['metadata'])
        server, wake, binding = _configuration(data)
        if selected is None or _revision(selected, binding) != expected_revision:
            return stale
    except (ValueError, OSError):
        return stale
    identity = selected['identity']
    result = {**base, 'selected': {'desktop_id': desktop_id, 'title': selected['title'],
                                 'workspace': label(identity['cwd'])}}
    target = {'session_id': identity['cliSessionId'], 'workspace': identity['cwd']}
    # Selection is never enrollment consent; another target cannot reuse an old record.
    if not binding or binding.get('target') != target:
        return {**result, 'status': 'native_authorization_unavailable',
                'message': 'This chat is not bound. This tool only inspects and has not requested host approval or enrolled the chat. A manual registration requires a legitimate current selected-chat receipt and authorized terminal access; preserve the existing binding until then.'}
    command = binding.get('command')
    expected = [str(root / '.venv/bin/python'), str(root / 'integrations/claude_code/bridge.py')]
    if (binding.get('adapter') != 'command' or not isinstance(command, list)
            or len(command) != 4 or command[:2] != expected or command[2] != '--session-record'
            or not isinstance(command[3], str)):
        raise ValueError('绑定适配器与此安装不匹配，请先核对安装。')
    try:
        confirmed_session(target, Path(command[3]))
        from .wakeup import parse_config
        bindings, options = parse_config(wake, server['peers'])
        if compatibility_issues(bindings, options):
            return {**result, 'status': 'stale_binding', 'message': 'The existing binding configuration needs review; preserve it.'}
    except (ValueError, OSError):
        return {**result, 'status': 'stale_binding',
                'message': 'The existing binding or recorded address is stale; preserve it. Supported native re-enrollment is required.'}
    try:
        current = _read_candidate(selected['metadata'])
        current_server, current_wake, current_binding = _configuration(data)
        if (current_server != server or current_wake != wake or current is None
                or _revision(current, current_binding) != expected_revision):
            return stale
    except (ValueError, OSError):
        return stale
    return {**result, 'status': 'existing_binding_verified',
            'message': 'The same existing binding passes local identity, configuration and record checks. Online status and message roundtrip require separate verification.'}
