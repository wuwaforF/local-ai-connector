# Architecture

This is the English map of the code for contributors. The detailed design history and host acceptance notes are in [PROJECT_NOTES.md](PROJECT_NOTES.md) and [GENERALIZATION.md](GENERALIZATION.md), mostly in Chinese.

## Processes and data flow

```text
initiating host ──stdio──▶ mcp_server.py ─▶ delegation.py ─▶ client.py ──HTTP──▶ /call, /decision ┐
worker host ─────stdio──▶ mcp_server.py ─────────────────▶ client.py ──HTTP──▶ /call             │
HTTP MCP host ────────────────────────────────────────────────────────────────▶ /mcp             │
                                                                                                  ▼
                                    server.py (Starlette, 127.0.0.1) ─▶ core.py Broker ─▶ state.sqlite3
                                                                          │ observers
                                                                          ▼
                                              wakeup.py Dispatcher ─▶ wakeup.sqlite3
                                                                          │ command / socket adapter
                                                                          ▼
                                             integrations/<host>/bridge.py ─▶ desktop app

owner ─▶ cli.py / ui.swift ─▶ /admin ─▶ Broker            (status, approve/deny/revoke for peer mode)
```

- **One service process owns the state.** `serve` holds an exclusive lock on the data directory before opening SQLite, so waiters are never split across processes.
- **stdio MCP processes are thin.** Each host starts its own `local-ai-connector mcp --config <endpoint.json>` process, which forwards tool calls to the service over loopback HTTP. Native identity checks run in this process, and in-chat approval is collected here before it is submitted to `/decision` with a separate approval credential.
- **Waits are event-driven.** The Broker wakes waiters through an `asyncio.Condition` on every state change. The wake dispatcher listens to the same condition, with a polling fallback (`poll_seconds`, default 5 s); while a message is settling it waits only until that message becomes ready.

## Boundaries

The cross-platform plan ([PLATFORM_PLAN.md](PLATFORM_PLAN.md)) separates three layers:

- **Shared core:** task authorization, binding rules, routing, state transitions and installation rules. The CLI and the MCP adapter only call its operations.
- **OS adapter (`os_adapter/`):** application directories, locks, ownership and permission checks, private and atomic file writes, detached process start. Only the module for the running platform (`posix.py` or `windows.py`) is imported, and capabilities that are not verified on a platform are reported as unsupported there.
- **Host adapters (`hosts/`):** MCP configuration install and removal, the trusted chat identity source, the approval transport, and user guidance for each host.

## Modules (`src/local_ai_connector`)

| Module | Responsibility |
| --- | --- |
| `cli.py` | Command entry point: `setup`, `uninstall`, `doctor`, `unbind`, `init`, `peer-add`, `enable-chat-approval`, `serve`, `mcp`, `client-config`, `service-config`, `status`, `approve`, `revoke`, `wakeup-status`, `call`, `file-*`, `model-*`, `ui`. |
| `server.py` | Starlette app with `/mcp` (stateless Streamable HTTP), `/call` (endpoint operations), `/decision` (approval submission with the approval credential) and `/admin` (owner). Handles Bearer auth, Origin and Host rejection, strict Pydantic request models and a 100,000-byte body limit. It starts the optional wake dispatcher. |
| `core.py` | `Broker`: endpoints, channels, messages, deduplication keys, approval decisions, expiry, incidents and waits. Every business rule is enforced here; `ConnectorError` carries machine-readable codes. |
| `client.py` | Async HTTP client used by stdio MCP processes. Proxies are disabled, and the HTTP timeout is the business wait plus 10 s. There are no automatic retries; callers resume with the same keys. |
| `mcp_server.py` | Tool definitions (official MCP Python SDK `MCPServer`) for the `peer`, `requester` and `participant` profiles. Also holds `en-US`/`zh-CN` instructions, the approval transport choice and the opt-in Codex/Claude binding tools and guide resources. |
| `delegation.py` | Runs a task in one tool call: open the channel, obtain approval (form elicitation or host tool permission), wait, surface follow-up questions, finish. It also handles resume with the same `request_key`. |
| `identity.py` | Native (Codex/ZCode) and configurable-metadata session identity for stdio endpoints, with first-use binding and conflict checks. |
| `registry.py` | Endpoint registration and profiles, private endpoint files and `enable_chat_approval`. |
| `bindings.py` | Exact-chat bindings per worker endpoint (revisioned, approved in the chat being bound), the target offered at task creation and pinned at approval, per-session receipt checks, and session-scoped in-memory delivery credentials. |
| `install.py` | Installation profiles under the platform data directory with an ownership manifest (`install.json`); idempotent `setup`, owned-only `uninstall`, `doctor`. |
| `service.py` | On-demand service start (detached), port-owner detection and owner-initiated stop. |
| `os_adapter/` | Platform primitives; see Boundaries. |
| `hosts/` | `codex.py` (`config.toml` through tomlkit), `claude.py` (`claude mcp add-json`/`remove`), `antigravity.py` (`mcp_config.json`). |
| `wakeup.py` | Optional wake-up: config parsing, `Dispatcher`, `WakeStore`, `CommandAdapter`. Intent is persisted before each side effect; unknown results are reconciled; targets are pinned per channel. |
| `adapter_socket.py` | `SocketAdapter`, plus a bridge host that serves a command bridge over a private Unix socket (for apps that inject their environment, such as the Antigravity sidecar). |
| `conversations.py` | Native-conversation providers (`codex_ingress`, `antigravity_sidecar`): exact single-use plans, hook receipts, saved Codex bindings and archive state. |
| `native_host.py` | Approved Antigravity create, send and archive operations through the sidecar. |
| `codex_binding.py` | Metadata-only catalog of existing Codex chats through the official Codex app server (quick binding). |
| `claude_binding.py` | Read-only Claude Desktop session catalog and binding inspection. |
| `workflows.py` | Owner-driven build → review workflows over ordinary approved channels. |
| `files.py` | Controlled file version and write: lock, digest check, atomic replace, path guards. |
| `supervisor.py` | Optional Chat Completions model that explains incidents. It cannot approve or change state. |
| `ui.swift` | macOS AppKit control window. It polls `/admin` every 2 s; the UI is Chinese. |

`integrations/` holds host-specific bridges and setup helpers (Codex Desktop IPC, Claude Desktop Code inbox, Antigravity `agentapi`). They run from the source checkout. `deployment/` holds maintainer scripts for a macOS LaunchAgent installation.

## Storage

An installation made by `setup` lives in `<application data>/profiles/<profile>/` (macOS `~/Library/Application Support/Local AI Connector`, Linux `$XDG_DATA_HOME/local-ai-connector`, Windows `%LOCALAPPDATA%\Local AI Connector`) and adds `install.json` (ownership manifest) and `backups/`. The legacy data directory (`.connector` in the quick start, `~/.local/share/local-ai-connector` for the macOS helpers) contains:

| File | Contents |
| --- | --- |
| `server.json` | port, URL, admin credential, endpoint credentials, profiles, `approval_tokens`, optional `conversations` and `codex_binding_cli` |
| `<endpoint>.json` | endpoint URL and credential, identity mode, tool profile, locale, approval transport and approval credential (mode 0600) |
| `state.sqlite3` | `peers`, `channels`, `messages`, `incidents`, `approval_decisions`, `chat_bindings`, `binding_requests`, `task_targets`, `native_*` provider tables, `workflow_runs`, `workflow_stages` |
| `wakeup.json`, `wakeup.sqlite3` | wake config; `dispatches`, `dispatch_messages`, `dispatch_events`, `delivered_messages`, `channel_targets`, `recoveries`, `quarantined_dispatch_messages` |
| `model.json` | optional supervisor model settings |

Both databases use WAL mode, and the message database enforces foreign keys. Schema changes are additive: tables and columns are created if missing, and there is no versioned migration system yet. The database stores credential digests only, but the JSON config files hold raw credentials, so protect the directory.

## Lifecycle of a delegated task

0. **Bind (once, changeable).** In the worker chat, `connector_bind_this_chat` asks for approval there. The chat is identified by the host (Codex request metadata, Claude session environment), never by the model. Each change creates a new binding revision; a binding grants no task authority.
1. **Create.** `connector_delegate` opens a `pending` channel. It is deduplicated by requester and `request_key`, and its authorization lasts 1 hour from creation. For a bound worker, the current chat and revision are offered for approval; on Claude they must be passed as `target_chat` and `binding_revision`, so the native permission prompt shows them.
2. **Approve.** The stdio process asks for approval in the initiating host and posts the decision to `/decision`. The approval record and the first question are written in one transaction, which also pins the offered chat and revision. If the binding changed after the task was created, approval is refused and nothing is delivered.
3. **Wake.** The dispatcher sees the new message. After `settle_seconds` (default 2 s, which gives an already-waiting worker a chance to read it first), it checks the pinned target's status. It then records intent and calls the adapter's `send`.
4. **Answer.** The worker calls `connector_receive`, and the read is recorded as delivery evidence. For a pinned task, only the pinned chat can receive or answer it; other chats of the same endpoint see nothing. It answers with `connector_send(kind="answer", reply_to=...)`, or asks with `kind="question"`.
5. **Return.** `delegate` returns `completed` with the answer, or `input_required`, `running` or another status. Callers resume with the same arguments.
6. **Continue or close.** `connector_continue` adds rounds on the same channel without extending its expiry. `finish` starts the idle timer (default 120 s). Expiry, decline and revoke are final.

## Where to start for common problems

| Symptom | Look at |
| --- | --- |
| Tools missing or calls time out early in a host | host MCP settings and timeouts, `client-config` output |
| Approval form never appears or cancels instantly | `delegation.py` capability negotiation, host form support, `approval_transport` |
| Worker not woken, woken twice or stuck | `wakeup-status`, `wakeup.py` (`_candidates`, `_gate`, `run`), bridge `status` output |
| Identity missing or conflicting | `identity.py`, endpoint file, host metadata |
| Reply mismatch, duplicates, expiry behaviour | `core.py` and `tests/test_core.py` |
| Native chat creation or archive | `conversations.py`, `native_host.py`, `integrations/*/native_*` |
