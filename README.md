# Local AI Connector

**English** | [简体中文](README.zh-CN.md)

Local AI Connector is a local MCP service that lets desktop AI agents hand tasks to each other over temporary, two-way channels. Codex, Claude Desktop Code, Antigravity, ZCode or any MCP host can take part. Every new task is shown to the user and approved in the desktop that started it. Delivery goes only to the exact chat registered for the target endpoint, and the real answer comes back to the originating chat.

**Status: developer preview, published to find collaborators.** The core service, approval and continuation flows have automated coverage. Several desktop round trips have been accepted by a person:

- Codex → Antigravity: one approval, automatic reply, continuation after a clarifying question.
- Codex → Claude Desktop Code: English task round trip.
- Claude → Antigravity: approved through Claude's per-call tool permission.

Host integrations rely partly on non-public desktop interfaces; see [Known limitations and help wanted](#known-limitations-and-help-wanted).

- Architecture and code map: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) · Security reports: [SECURITY.md](SECURITY.md)
- Detailed design history and acceptance notes (Chinese): [docs/PROJECT_NOTES.md](docs/PROJECT_NOTES.md), [docs/GENERALIZATION.md](docs/GENERALIZATION.md)

## How it works

```text
Initiating chat ──MCP──▶ connector (127.0.0.1) ──wake adapter──▶ worker chat
      ▲                    │  approval shown in the initiating desktop
      └──── real answer ◀──┘  worker replies with connector_send
```

1. The initiating agent calls `connector_delegate(target, message, request_key, conversation_mode)`.
2. The user sees the target and the full task text in their own desktop and approves or declines it. This uses an MCP form, or the host's per-call tool permission where forms are unavailable.
3. Once approved, the connector stores the message. An optional wake adapter asks the registered worker chat to collect it.
4. The worker reads the task with `connector_receive` and answers with `connector_send`. It can also ask a follow-up question.
5. The original call returns the worker's actual answer. Later rounds use `connector_continue` on the same channel and reuse the original approval until it expires.

Approval covers this task's communication only. File edits and commands run by a worker remain under that worker host's own permissions.

## Requirements

- macOS (verified on Apple Silicon), Python 3.13 (`requires-python >= 3.11`). Windows is not supported because of the Unix file locks.
- [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Optional: macOS Command Line Tools (`xcrun swiftc`) for the native control window.

## Quick start

From a clone of this repository:

```sh
uv sync --frozen --extra test
uv run local-ai-connector --data .connector init --peers writer reviewer tester
uv run local-ai-connector --data .connector serve
```

- The service listens on `127.0.0.1:38471` by default. Use `--port` and another data directory for a separate instance.
- `--peers` takes two or more names; without it, `worker-a` and `worker-b` are created. `server` and `model` are reserved.
- Each endpoint gets its own credential.
- The data directory holds credentials and message history. It is excluded from Git.

Keep the service running. In another terminal, generate an MCP entry for each host and merge it into that host's MCP settings:

```sh
uv run local-ai-connector --data .connector client-config --peer writer                  # generic mcpServers JSON
uv run local-ai-connector --data .connector client-config --peer reviewer --client codex # Codex TOML
uv run local-ai-connector --data .connector client-config --peer tester --client zcode   # ZCode JSON
```

- `--client` only changes the output format; it never edits host settings.
- The output contains absolute paths to Python and the endpoint file, so regenerate it after moving the installation.
- Generated entries use a 60 s host tool timeout to match the connector's default 20 s wait.

For a peer-mode task (approval outside the chat), approve from the terminal or the native window:

```sh
uv run local-ai-connector --data .connector approve   # shows the original request; type yes to approve
uv run local-ai-connector --data .connector status
uv run local-ai-connector --data .connector ui        # macOS control window (currently Chinese UI)
```

On macOS, `service-config` prints a LaunchAgent plist for running the service in the background. Installing and loading it is left to you.

## Endpoints and tool profiles

Register more workers while the service is stopped:

```sh
uv run local-ai-connector --data .connector peer-add proofreader \
  --name 'Proof reader' --description 'Checks facts and wording' \
  --capability fact-check --capability proofreading --mcp-locale en-US
```

`connector_status` returns a `workers` catalog: use `id` as the delegation target; `self` is the caller. Capabilities are administrator-declared, and `availability: unknown` does not mean online. `--mcp-locale en-US` switches that stdio endpoint's MCP instructions and tool descriptions to English (default `zh-CN`). Message content is never translated.

| Profile | Tools | Use |
| --- | --- | --- |
| `peer` (default stdio, Streamable HTTP) | `status`, `request_help`, `continue`, `receive`, `send`, `finish` | low-level protocol; approval in the control window or CLI |
| `requester` | `status`, `delegate`, `continue`, `archive` | starts tasks with in-chat approval |
| `participant` | requester tools + `receive`, `send`, `finish` | both starts and receives tasks |

All tool names carry the `connector_` prefix. Enable in-chat approval for registered endpoints (service stopped) with:

```sh
uv run local-ai-connector --data /absolute/path/to/data enable-chat-approval writer reviewer --mcp-locale en-US
```

This creates a separate approval credential per endpoint and switches it to `participant`. It keeps the endpoint's identity, and refuses to run while the service is up or when settings conflict. Restart the service and reload MCP in the host afterwards.

**Approval transports.**

- `elicitation` (default) uses a standard MCP form, so the host must actually render forms.
- `approval_transport: "host_tool_permission"` in a participant's endpoint file marks `connector_delegate` as requiring user interaction on every call. The host's native allow/deny prompt then carries the approval. This is used for Claude Desktop Code.

In both cases the model cannot approve through tool arguments.

### Calling conventions

- Find the target with `connector_status`, then call `connector_delegate(target, message, request_key, conversation_mode)`. Decline or cancel returns that decision, and nothing is delivered.
- **Timeouts:** the execution wait defaults to 180 s (`timeout_seconds` 1–600, counted from approval). The approval wait is at most 300 s, and the task authorization lasts 1 hour from creation.
- **Resuming:** on `running`, `approval_timeout` or an interrupted call, repeat the same `request_key`, `target`, `message` and `conversation_mode` to resume. Do not create a new task.
- **Follow-up questions:** on `input_required`, answer by calling again with `reply_to` (the question `id`) and `reply`.
- **Next rounds:** use `connector_continue(channel, message, request_key)`. A channel closed for inactivity can resume only within the original authorization; expired, revoked or declined channels never reopen.
- **Result:** in a `completed` result, `answer.body` is the worker's actual reply.

`conversation_mode` is required:

| Intent | Call | Result |
| --- | --- | --- |
| New task for an existing worker chat | `delegate(..., conversation_mode="existing")` | new channel, registered chat |
| User explicitly asks for a new chat | `delegate(..., conversation_mode="new")` | only for targets with a registered native-conversation provider |
| Follow-up on the same task | `continue(channel=..., ...)` | reuses the original approval and chat |

### Example: Codex as requester

```toml
[mcp_servers.local_ai_connector]
command = "/absolute/path/to/local-ai-connector/.venv/bin/python"
args = ["-m", "local_ai_connector.cli", "mcp", "--config", "/absolute/path/to/data/writer.json"]
startup_timeout_sec = 20
tool_timeout_sec = 660
enabled_tools = ["connector_status", "connector_delegate", "connector_continue", "connector_archive"]

[mcp_servers.local_ai_connector.tools.connector_delegate]
approval_mode = "approve"
```

Repeat the `tools.<name>` table for each enabled tool. `approval_mode = "approve"` only allows calling the tool; each new task is still approved separately in the form. Keep credentials out of prompts and tool arguments.

### Example: Antigravity worker permissions

In **Settings → Projects → (project) → MCP Tools**, add **Allow** rules for `local_ai_connector/connector_receive` and `local_ai_connector/connector_send`. The server name must match the host's registered MCP server. This lets the worker collect approved tasks and return answers. Task approval still happens in the initiating chat.

### Streamable HTTP

The same `serve` process exposes `http://127.0.0.1:38471/mcp` with the peer tools:

```sh
uv run local-ai-connector --data .connector client-config --peer reviewer --transport streamable-http
```

- Each request is authenticated with the endpoint's Bearer credential. The admin credential cannot call `/mcp`.
- Browser `Origin` headers and unexpected `Host` values are rejected.
- The transport is stateless, with state kept in SQLite.
- This is for trusted local clients only: no public deployment, OAuth or cloud relay.

## Identity modes

- **`generic` (default):** the endpoint is identified by its credential. Two chats sharing one credential share one inbox, so give each chat its own endpoint.
- **Native (`--native-peer name=codex|zcode`):** the task ID comes from host metadata, with first-use binding and conflict checks. stdio only.
- **`metadata`:** `peer-add ... --session-namespace NS --session-path a b c` reads a trusted ID from nested MCP request metadata. stdio only, and missing fields fail explicitly.

## Wake adapters and native conversations

Registration does not mean a worker is online. A worker can wait with `connector_receive`, or an optional wake adapter in `<data>/wakeup.json` can nudge a registered chat after approval:

- **`command` adapter:** runs a bridge with one JSON request on stdin.
- **`socket` adapter:** calls a bridge over a private Unix socket.

Bridges implement `confirm`, `status`, `send`, `reconcile` and, optionally, `restore`. Unknown send results are reconciled, never blindly retried. Bundled bridges:

| Host | Bridge | Notes |
| --- | --- | --- |
| Codex Desktop | `integrations/codex_desktop/bridge.py` | pinned thread via the desktop's local IPC; optional cold-start `restore` |
| Claude Desktop Code | `integrations/claude_code/bridge.py` | captured session inbox; status is `unknown`, so it needs `send_when_unknown: true` |
| Antigravity | `integrations/antigravity/bridge.py` | via `agentapi` inside an Antigravity sidecar; status `unknown` |

Native-conversation providers (`codex_ingress`, `antigravity_sidecar`) in `server.json` allow `conversation_mode="new"` and approved archiving through `connector_archive`. See [examples/configs](examples/configs/README.md) for placeholder `wakeup.json`, provider, launcher and sidecar files.

Opt-in binding helpers (MCP server flags):

- `--codex-quick-binding`: lets an initiating desktop pick an existing Codex chat by title and save it as the target. The tools are `connector_codex_binding_catalog`, `connector_codex_bind_chat`, `connector_codex_bound_chat` and `connector_codex_quick_bind`. Saving needs approval and sends nothing; each later task is approved again. Guide: [codex_binding_guide.md](src/local_ai_connector/codex_binding_guide.md).
- `--claude-binding-root /absolute/path/to/checkout`: adds read-only Claude session inspection with `connector_claude_binding_catalog` and `connector_claude_binding_inspect`. Guide: [claude_binding_guide.md](src/local_ai_connector/claude_binding_guide.md). For a new Claude project entry, the runtime must live outside `~/Documents`, because Claude cannot read that folder by default.

Owner-driven build → review workflows (`connector_workflow_start/status/resume/cancel`) chain approved stages between two registered endpoints; see [docs/WORKFLOWS.md](docs/WORKFLOWS.md) (Chinese).

## File collaboration

The connector does not apply worker output to your files. For parallel code changes, give each worker its own `git worktree` and exchange commits or `git diff --binary` patches.

When several workers must update one shared file, use the controlled write path:

```sh
uv run local-ai-connector file-version --root /path/to/workspace shared.txt
uv run local-ai-connector file-write --root /path/to/workspace --source new.txt --expected <digest|missing> shared.txt
```

Each write takes a process lock, checks the expected content digest and replaces the file atomically. It rejects paths outside the root, symlinks, hard links and files over 10 MiB. Editors and other tools do not honour this lock, so it is not an isolation boundary.

## Optional supervisor model

`model-config`, `model-test` and `model-explain <incident>` connect any Chat Completions-compatible endpoint to explain recorded incidents. Explanations are currently in Chinese.

- Cloud endpoints must use HTTPS.
- The model cannot approve, change state or write files.
- No default model is shipped. The model is only an aid and does not decide anything.

Re-run `examples/evaluate_supervisor.py` to test your own model.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/local_ai_connector` | the Python package: service, MCP server, delegation, wake dispatcher, files, supervisor |
| `integrations/` | host bridges and setup helpers; they run from the source checkout and are not in the wheel |
| `deployment/` | maintainer macOS scripts; some preview by default and need `--apply`, others act immediately, so read the header first |
| `examples/configs/` | placeholder configuration files, loaded by the real parsers in the tests |
| `research/` | standalone MCP form and tool-permission probes |
| `docs/` | architecture (English) and design history (Chinese) |

The helpers in `integrations/` and `deployment/` currently assume:

- a source checkout prepared with `uv sync`
- the data directory `~/.local/share/local-ai-connector`
- the LaunchAgent label `dev.local-ai-connector.service`
- the endpoint names `gpt` (Codex requester), `codex_desktop`, `gemini` (Antigravity) and `claude_code`

The core service accepts any names and data directory. Scripts that target a specific chat or project take it explicitly:

```sh
deployment/register-codex-desktop.command --thread-id <codex-thread-id> --workspace /absolute/path/to/workspace
deployment/enable-codex-desktop-cold-restore.command --thread-id <codex-thread-id> --workspace /absolute/path/to/workspace
deployment/migrate-codex-communication-permissions.command --worker-project /absolute/path/to/workspace --antigravity-project-id <project-id>
```

Credentials, host MCP settings, SQLite state and logs stay in the data directory and in each host's private settings. Never commit them.

## Testing

```sh
uv run pytest -q
```

Tests use temporary directories and ports and do not touch desktop accounts or an installed connector. Some tests bind local TCP ports and Unix sockets. CI runs the suite on macOS.

They cover authorization visibility, identity binding, message correlation, idempotency, follow-up questions, revoke, cancel, expiry, restart recovery, and real stdio and Streamable HTTP MCP sessions. They also cover multi-endpoint isolation, concurrent writers, path boundaries and the wake dispatcher.

After upgrading, restart the service and reload stdio MCP servers in each host so long-lived processes pick up the new code. Never run two services on one data directory; a lock prevents it.

## Known limitations and help wanted

- **macOS only.** Windows needs a replacement for the Unix file locks and sockets.
- **Packaging:** the integration helpers use fixed endpoint names, a fixed data directory and a fixed LaunchAgent label, and they run from the source `.venv`. Packaging `integrations/` and making these configurable would allow a plain `uv tool install`.
- **Host fragility:** the Codex Desktop IPC, Antigravity internal RPC and Claude Desktop session details are non-public and can change with host updates. Per-version contract tests and early "unsupported host version" errors are needed.
- **Progress feedback:** MCP progress notifications during `delegate` waits (approved, delivered, read, answered) would make long tasks feel smoother.
- **Localization:** the CLI messages and the control window are Chinese only, and English output is welcome. The dated notes in `docs/` are Chinese and could be summarized in English.
- **Pending acceptance:** a person has not yet accepted automatic cold-start recovery of Codex Desktop, or LaunchAgent auto-load after login.
- **Isolation:** file permissions protect the data directory, but other processes running as the same OS user are not isolated from it.

## License

MIT. Third-party dependencies keep their own licenses; research sources and notices are listed in [NOTICE.md](NOTICE.md).
