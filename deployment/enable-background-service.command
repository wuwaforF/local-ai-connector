#!/bin/zsh
# Run in macOS Terminal: the agent sandbox cannot manage launchd or signal the old service.
set -eu
setopt PIPE_FAIL

connector_root="${0:A:h:h}"
connector_data="$HOME/.local/share/local-ai-connector"
connector_python="$connector_root/.venv/bin/python"
connector_plist="$HOME/Library/LaunchAgents/dev.local-ai-connector.service.plist"
connector_label="gui/$(id -u)/dev.local-ai-connector.service"

# Verify the installed definition and the listener before taking over an existing process.
connector_old_pid="$("$connector_python" - "$connector_root" "$connector_data" "$connector_plist" <<'PY'
import json, os, pathlib, plistlib, sqlite3, subprocess, sys
root, data, plist = map(pathlib.Path, sys.argv[1:])
config = plistlib.loads(plist.read_bytes())
expected = [str(root / '.venv/bin/python'), '-m', 'local_ai_connector.cli', '--data', str(data), 'serve']
if (config.get('Label') != 'dev.local-ai-connector.service'
        or config.get('ProgramArguments') != expected
        or config.get('EnvironmentVariables', {}).get('PYTHONPATH') != str(root / 'src')
        or config.get('KeepAlive') is not True or config.get('RunAtLoad') is not True):
    raise SystemExit('停止：已安装的后台配置与当前安装不一致。')
with sqlite3.connect((data / 'state.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
    if db.execute("SELECT count(*) FROM channels WHERE status IN ('pending','active')").fetchone()[0]:
        raise SystemExit('停止：仍有未结束的任务，请任务结束后再运行。')
port = json.loads((data / 'server.json').read_text())['port']
result = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t'], capture_output=True, text=True)
if result.returncode not in (0, 1):
    raise SystemExit(result.stderr)
pids = set(result.stdout.split())
if len(pids) > 1:
    raise SystemExit('停止：端口存在多个监听进程，需先核对。')
pid = next(iter(pids), '0')
if pid != '0':
    uid = subprocess.check_output(['/bin/ps', '-p', pid, '-o', 'uid='], text=True).strip()
    command = subprocess.check_output(['/bin/ps', '-ww', '-p', pid, '-o', 'command='], text=True).strip()
    matches = command == ' '.join(expected)
    if command == ' '.join(['.venv/bin/python', *expected[1:]]):
        cwd = subprocess.check_output(['/usr/sbin/lsof', '-a', '-p', pid, '-d', 'cwd', '-Fn'], text=True)
        matches = 'n' + str(root) in cwd.splitlines()
    if uid != str(os.getuid()) or not matches:
        raise SystemExit('停止：现有监听进程与此连接服务不一致。')
print(pid)
PY
)"

/usr/bin/plutil -lint "$connector_plist"
if /bin/launchctl print "$connector_label" >/dev/null 2>&1; then
    print '同名服务已经注册，本文件未做切换。请回到 Codex 核对已有服务。'
    exit 1
fi
# Register first; if registration fails, the existing service stays available.
/bin/launchctl bootstrap "gui/$(id -u)" "$connector_plist"

if [[ "$connector_old_pid" != 0 ]]; then
    /bin/kill -TERM "$connector_old_pid"
fi

connector_port="$("$connector_python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$connector_data/server.json")"
connector_new_pid=""
for connector_attempt in {1..40}; do
    connector_new_pid="$(/usr/sbin/lsof -nP -iTCP:"$connector_port" -sTCP:LISTEN -t || [[ $? == 1 ]])"
    connector_job_pid="$(/bin/launchctl list | /usr/bin/awk '$3 == "dev.local-ai-connector.service" {print $1}')"
    if [[ -n "$connector_new_pid" && "$connector_new_pid" != "$connector_old_pid" && "$connector_new_pid" == "$connector_job_pid" ]]; then
        break
    fi
    sleep 1
done
if [[ -z "$connector_new_pid" || "$connector_new_pid" == "$connector_old_pid" || "$connector_new_pid" != "$connector_job_pid" ]]; then
    print -u2 '后台服务尚未接管。请保留本窗口中的错误信息。'
    /bin/launchctl print "$connector_label"
    exit 1
fi
if [[ "$(/bin/ps -p "$connector_new_pid" -o uid= | /usr/bin/tr -d ' ')" != "$(id -u)" || "$(/bin/ps -ww -p "$connector_new_pid" -o command=)" != "$connector_python -m local_ai_connector.cli --data $connector_data serve" ]]; then
    print -u2 '新服务身份不符合配置，停止恢复测试。'
    exit 1
fi
print "后台服务已监听，PID=$connector_new_pid。现在验证自动恢复；请勿发起新任务。"

"$connector_python" - "$connector_data" <<'PY'
import pathlib, sqlite3, sys
data = pathlib.Path(sys.argv[1])
with sqlite3.connect((data / 'state.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
    if db.execute("SELECT count(*) FROM channels WHERE status IN ('pending','active')").fetchone()[0]:
        raise SystemExit('已有新任务：保留已接管的后台服务，本次不做恢复测试。')
PY

/bin/launchctl kill SIGTERM "$connector_label"
connector_recovered_pid=""
for connector_attempt in {1..40}; do
    connector_recovered_pid="$(/usr/sbin/lsof -nP -iTCP:"$connector_port" -sTCP:LISTEN -t || [[ $? == 1 ]])"
    connector_job_pid="$(/bin/launchctl list | /usr/bin/awk '$3 == "dev.local-ai-connector.service" {print $1}')"
    if [[ -n "$connector_recovered_pid" && "$connector_recovered_pid" != "$connector_new_pid" && "$connector_recovered_pid" == "$connector_job_pid" ]]; then
        break
    fi
    sleep 1
done
if [[ -z "$connector_recovered_pid" || "$connector_recovered_pid" == "$connector_new_pid" || "$connector_recovered_pid" != "$connector_job_pid" ]]; then
    print -u2 '自动恢复未通过，请保留本窗口中的错误信息。'
    /bin/launchctl print "$connector_label"
    exit 1
fi
if [[ "$(/bin/ps -p "$connector_recovered_pid" -o uid= | /usr/bin/tr -d ' ')" != "$(id -u)" || "$(/bin/ps -ww -p "$connector_recovered_pid" -o command=)" != "$connector_python -m local_ai_connector.cli --data $connector_data serve" ]]; then
    print -u2 '恢复后的进程身份不符合配置，请保留本窗口信息。'
    exit 1
fi
print "自动恢复完成，PID=$connector_recovered_pid。请回到 Codex 继续核对 MCP 连接。"
/bin/launchctl print "$connector_label"
