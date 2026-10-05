#!/bin/zsh
set -eu
connector_root="${0:A:h:h}"
exec "$connector_root/.venv/bin/python" "$connector_root/integrations/codex_desktop/conversation_deployment.py" "$@"
