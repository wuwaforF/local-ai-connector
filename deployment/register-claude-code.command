#!/bin/zsh
# No arguments opens the guided picker; explicit flags retain the advanced CLI.
set -eu
connector_root="${0:A:h:h}"
if [[ ! -x "$connector_root/.venv/bin/python" ]]; then
    print -u2 '请先在此连接器目录安装 Python 运行环境，然后重新打开绑定向导。'
    print -u2 '安装说明：README.md；会话绑定说明：docs/CLAUDE_ONBOARDING.md。'
    exit 1
fi
exec "$connector_root/.venv/bin/python" "$connector_root/integrations/claude_code/onboarding.py" "$@"
