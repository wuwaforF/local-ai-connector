# Antigravity chat-identity diagnostic

These scripts check whether a connector can tell which Antigravity chat is calling an MCP tool.
The question is who is calling, not whether the connector can send to a conversation. Everything
runs in a disposable workspace: Antigravity loads the probe server and hooks from that workspace's
`.agents/` folder only. Global host configuration and any existing connector installation stay
untouched.

| Script | Role |
| --- | --- |
| `probe_server.py` | Stdio MCP server whose `identity_probe` tool records the arguments, request `_meta`, protocol and serving process |
| `hook.py` | Lifecycle hook. Records the host payload (`conversationId`, `toolCall`). For probe calls it tries an `overwrite` with a nonce. `--deny-server NAME` blocks a server in the workspace. |
| `make_workspace.py` | Writes the workspace's `.agents/mcp_config.json` and `.agents/hooks.json` |
| `analyze.py` | Correlates hook records with received calls |
| `prepare_connector_test.py` | Adds an isolated connector profile (`setup` under a test home) to the workspace and blocks the live server there |
| `initiator.py` | Terminal stand-in for an initiating desktop. Only a typed `yes` approves a task. |

```sh
.venv/bin/python -m pytest -q research/antigravity-identity/test_probe.py -p no:cacheprovider
.venv/bin/python research/antigravity-identity/make_workspace.py ~/Developer/agy-identity-probe
.venv/bin/python research/antigravity-identity/analyze.py ~/Developer/agy-identity-probe/.probe
```

Results from 2026-10-05 on Antigravity 2.19.1, macOS, are in `docs/PLATFORM_PLAN.md` section 4.4:

- Every MCP `tools/call` carried `_meta["antigravity.google/conversation_id"]`.
- It matched the documented hook `conversationId` for every call.
- It distinguished two chats served by one MCP process, and it survived a host restart.

Delete the workspace folder to remove the diagnostic.
