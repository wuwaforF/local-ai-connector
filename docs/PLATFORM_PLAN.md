# Cross-platform first release: architecture and phased plan

Status: **agreed direction, revised 2026-10-05.** Increment 1 passes automated checks on macOS, Ubuntu
and Windows CI. Nothing has been verified on a real desktop on any OS.

Product goal: install the connector once in each supported desktop host, bind an existing chat in
natural language, approve each task in the initiating desktop, and receive the result from the
exact bound chat.

## 1. Fixed constraints

### 1.1 Safety contract

1. **Approval stays in the initiating desktop.**
   - Every new task is approved there.
   - Task approval is separate from MCP tool permissions and filesystem permissions. Setup never
     grants tool or file permissions on its own; it only guides the user through them.
2. **The target is pinned at authorization.**
   - Approving a task pins the target chat and the binding revision that the approval showed.
   - Re-binding affects only future authorizations. It never redirects a task that was already
     approved.
   - If the binding changes between the request and the decision, the approval is refused and
     nothing is delivered.
3. **Exact-chat identity is trusted.**
   - Binding and task receipt use a session identity supplied by the host, never by the model.
   - Shared endpoint credentials or model-supplied session IDs alone are not enough.
   - If a host cannot supply a reliable identity, we document that and do not claim exact-chat
     support for it.
4. **Bindings carry no authority.** A binding grants no task authority, and an incoming message
   from another process can never stand in for user consent.

### 1.2 Engineering rules

1. Keep the existing Python/MCP stack and its messaging, persistence, deduplication and approval
   logic. Do not assume the architecture already works on every platform: each claim needs
   evidence.
2. File-safety guarantees survive the port. A primitive is used on an OS only after its locking,
   permission or write guarantee has been verified there. Anything unverified is reported as
   unsupported on that OS.
3. The repository stays private, and the live installation stays untouched. No live host
   configuration is changed before the real-desktop acceptance environments are chosen.

## 2. Architecture

```text
            CLI / MCP stdio adapter / (future UI)          ← thin; call core operations only
                              │
                 ┌────────────▼────────────┐
                 │       shared core       │  task authorization · binding rules · routing
                 │ core.py  bindings.py    │  state transitions · dedup · persistence
                 │ delegation.py wakeup.py │  install manifest and ownership rules
                 │ install.py              │
                 └──────┬───────────┬──────┘
                        │           │
          ┌─────────────▼──┐     ┌──▼─────────────────────────┐
          │  OS adapter    │     │  host adapters             │
          │  os_adapter/   │     │  hosts/codex.py            │
          │   posix.py     │     │  hosts/claude.py           │
          │   windows.py   │     │  hosts/antigravity.py      │
          └────────────────┘     └────────────────────────────┘
```

### 2.1 OS adapter

One small package with a POSIX module and a Windows module. The module for the running OS is
loaded, and every capability reports whether it is supported. It provides:

- application directories;
- an exclusive file lock;
- ownership and permission checks;
- private file and directory creation;
- atomic private replacement;
- detached process start;
- local IPC transports owned by this project.

### 2.2 Host adapters

One small module per host, holding only what differs between hosts:

- MCP configuration installation and removal;
- the trusted session identity source;
- chat discovery and binding inputs;
- the initiating-approval transport;
- the wake-up and delivery contract, including the host's own inbox transport and
  authentication;
- host version checks.

A host adapter that needs an OS-specific transport, such as a named pipe for Claude on Windows,
gets it from the OS adapter. There is no plugin framework, no marketplace, and no nine separate
host × OS implementations.

### 2.3 Shared core

- **Task lifecycle (`core.py`):** authorization, deduplication and messages. It adds the task's
  target pin and per-call session checks.
- **Bindings (`bindings.py`, new):** a binding revision for each worker endpoint; bind requests
  and their approval; target pins taken at authorization; session-scoped delivery credentials
  that live only in memory and are invalidated when stale.
- **Delegation (`delegation.py`):** approval prompts that show the pinned target.
- **Wake-up (`wakeup.py`):** the dispatcher. From Increment 2 it delivers only to the pinned
  target.
- **Installation (`install.py`, new):** profiles, the ownership manifest, collision detection,
  idempotent setup and owned-only uninstall. The CLI and MCP adapter call these operations; they
  hold no rules of their own.

## 3. Release decisions (adopted)

1. **Release shape.**
   - Attended mode is available everywhere as a fallback and diagnostic path. It does not fulfil
     the goal of automatic collaboration after one install.
   - Automatic wake-up is offered only where it has been verified.
   - Support is reported per **host × OS × mode** with the host versions tested, and automated
     checks are kept separate from real-desktop verification.
2. **CI and acceptance order.**
   - Core CI runs on macOS, Windows and Ubuntu from Increment 1.
   - Real-desktop acceptance goes macOS → Windows → Linux.
   - Windows core portability is not deferred.
3. **Codex automatic wake-up on macOS** is on by default, with `setup codex --no-wake` to turn it
   off (owner decision, 2026-10-10; it was first adopted as an opt-in experiment). It relies on
   capability checks, and a host-version contract is still to come. A documented app-server
   `turn/start` is not evidence that it can wake a chat owned by the desktop app.
4. **Claude on Windows.** A session hook may hand the session's messaging token to the service,
   with these conditions:
   - It applies only to a target session the user explicitly enrolled.
   - The token stays scoped to that session, and its local transfer and storage are protected.
   - Stale tokens are invalidated.
   - The token never appears in logs, prompts, diagnostics or the repository.
   - Authenticated own-child messaging must never stand in for user consent or override inbound
     refusal.
5. **Isolation from the live installation.**
   - Each new installation gets an installation ID and profile, platform directories and an
     ownership manifest.
   - Collisions are detected, never assumed away.
   - Setup is idempotent, and uninstall removes only resources that installation owns.
   - Migrating the legacy installation is deferred.
6. **Host configuration.** `tomlkit` handles Codex TOML edits that must preserve comments. A
   supported host command is used wherever it meets the requirements: `claude mcp add-json` and
   `claude mcp remove`.
7. **CI cost.** Ubuntu and Windows runs use standard runners within the included quota and the
   configured spending limits. Jobs are bounded and superseded runs are cancelled. Paid overages,
   larger runners and billing changes need separate approval.

## 4. Assessment

The detailed evidence was gathered 2026-10-05.

| Tag | Meaning |
| --- | --- |
| [doc] | official documentation |
| [obs] | observed read-only on the maintainer Mac |
| [code] | this repository |
| [3p] | third party or changelog; unconfirmed |

### 4.1 Core portability [code]

- **Native Windows: blocked today.**
  - `registry.py`, `identity.py`, `files.py` and `adapter_socket.py` import `fcntl` at module
    level. `cli.py`, `mcp_server.py` and `server.py` import them, so the CLI, the MCP entry and
    the service fail at import.
  - Bridges use `AF_UNIX`, which CPython does not support on Windows [doc].
  - Owner and mode checks use `os.getuid` and POSIX mode bits. Windows reports regular files as
    `0o666`, so a valid `wakeup.json` would be rejected.
  - `files.py` depends on `O_NOFOLLOW` and `dir_fd`.
- **Portable already:** `core.py`, `client.py`, `delegation.py`, `conversations.py`,
  `workflows.py`, the wake dispatcher and the dependency lock, which includes Windows and
  manylinux wheels.
- **Tests and CI:** 222 of 465 test functions are in files that use POSIX- or macOS-specific APIs.
  CI was macOS-only.

### 4.2 Hosts by platform

| Host | macOS | Windows | Linux | WSL |
| --- | --- | --- | --- | --- |
| Codex app (inside the ChatGPT desktop app) | Available | Available; the agent runs natively or in WSL 2 [doc](https://learn.chatgpt.com/docs/windows/windows-app) | Preview since 2026-08 [forum](https://community.openai.com/t/codex-in-chatgpt-desktop-app-for-linux-is-now-in-preview/1390027) | Windows app in WSL agent mode; CLI in the distro |
| Claude Desktop, Code tab | Available | Available [doc](https://code.claude.com/docs/en/desktop) | Beta, Debian/Ubuntu only [doc](https://code.claude.com/docs/en/desktop-linux) | Desktop "WSL sessions" run Claude Code in the distro; no connectors or plugins there [doc](https://code.claude.com/docs/en/desktop-wsl) |
| Antigravity | Available | Available | Available [doc](https://antigravity.google/docs/getting-started/) | Not documented |

WSL is reported separately:

- A connector installed **inside** a distro is a Linux install. It serves agents whose processes
  run in that distro.
- Native Windows agents use a native Windows install.
- Joining the two is out of scope for the first release. Claude documents that WSL 2 sessions and
  native Windows sessions cannot message each other.

### 4.3 Host capabilities

| | Codex | Claude Desktop Code | Antigravity |
| --- | --- | --- | --- |
| **Config install** | `config.toml` `[mcp_servers.<id>]` with timeouts, `enabled_tools` and per-tool `approval_mode` [doc]. `codex mcp add` cannot set timeouts, so files are edited with `tomlkit`. | `claude mcp add-json --scope user` / `claude mcp remove` [doc]. The CLI ships inside the app [obs]. | `~/.gemini/config/mcp_config.json` [doc]; no CLI |
| **Trusted chat identity** | `_meta["x-codex-turn-metadata"]["thread_id"]` on each tool call [obs, code]. MCP servers get a sanitized environment [obs]. | `CLAUDE_CODE_SESSION_ID` in the stdio MCP child [3p changelog]; session ID in hook input [doc]. One MCP process runs per session. `_meta["antigravity.google/conversation_id"]` on each tool call [obs, 2.19.1], matching the documented hook payload's `conversationId` [doc](https://antigravity.google/docs/hooks). One MCP process serves all chats, so it is read per call. See section 4.4. |
| **Initiating approval** | MCP form. Verified on macOS. | Native per-call tool permission. Verified on macOS. | MCP form. Shown on macOS, but hit the host's 180 s tool deadline. |
| **Wake-up / delivery** | Desktop IPC (non-public; macOS experiment). App-server `turn/start` is documented, but sharing a thread with the desktop app is not. | Per-session inbox [doc](https://code.claude.com/docs/en/cross-session-messaging). Unix socket (auth optional) on macOS, Linux and WSL 2; named pipe with **required** token on Windows. The frame format is undocumented. Inbound controls can hold or refuse messages. | Sidecar plus `agentapi send-message` [doc](https://antigravity.google/docs/sidecars/). `agentapi` on Windows and Linux is unconfirmed. |

### 4.4 Antigravity chat identity: validated on macOS (2026-10-05)

An earlier version of this plan concluded that no identity reached Antigravity's MCP servers.
That checked only the server process environment and was wrong. The identity is carried per
call.

**Probe** (`research/antigravity-identity/`, in a disposable workspace whose `.agents/` held the
probe server and hooks; no global configuration changed). Two chats, five calls, including a full
Antigravity restart:

- Every `tools/call` carried `_meta["antigravity.google/conversation_id"]`, plus
  `antigravity.google/artifacts_dir` and `progress_token`.
- That value matched the documented `PreToolUse` hook `conversationId` and the hook's
  `ANTIGRAVITY_CONVERSATION_ID` on all five calls.
- Both chats were served by one MCP process, and conversation IDs were unchanged after the restart.
- MCP calls reach hooks as `call_mcp_tool`, with the arguments under `Arguments`.
- An undocumented `PreToolUse` `overwrite` replaced a model-supplied value before execution.

**Connector test** (isolated profile `agtest`, entry loaded only by that workspace, and a workspace
hook denying the live server):

- Chat A bound itself through Antigravity's form (revision 1), received task t1 and answered `42`.
  The initiator was a terminal stand-in, and each task was approved by typing `yes`.
- Chat B's inbox showed nothing of t1. B's explicit receive and its answer for t1 were refused
  with `wrong_session`.
- After t2 was approved for A, B bound itself (revision 2), and t3 was approved for B.
- After a full Antigravity restart:
  - B saw only t3, its explicit read of t2 was refused, and it answered `10`.
  - A saw only t2 and answered `7`.
  - All three tasks completed at the initiator.
- Evidence agrees across the profile database (bindings, pins, message senders, three
  `wrong_session` incidents), the hook log (the host's conversation for every call) and both chat
  transcripts. A change in Antigravity's per-launch browser-control URL confirms the restart.

**Limits:**

- The `_meta` key is undocumented and was observed on 2.19.1 only. A host version that drops it
  makes binding fail with `missing_session_identity`; it never falls back to an unverified
  identity.
- The documented hook `conversationId` could serve as a second, documented source.
- Approvals in this test were human, but the initiating side was a terminal stand-in, not a desktop.
- Not yet run on Windows or Linux Antigravity.

### 4.5 Codex direct wake-up of the pinned chat: validated on macOS (2026-10-09)

Codex Desktop 26.1002.52244 (bundled CLI 0.162.0-alpha.2), macOS. Code under test: PR #13
(`research/codex-direct-wake/`). No relay chat was involved.

**Isolation.** `setup codex --profile cwtest` ran under a throwaway home. Its entry was loaded only
through the test folder's project `.codex/config.toml`, which also set the live `local_ai_connector`
to `enabled = false`. Codex applies project config only in a trusted folder; with the bundled CLI the
override held in the trusted folder and not elsewhere or in an untrusted copy. In Desktop, the test
chats listed only `local_ai_connector_cwtest`, and every connector call went through it. The global
`~/.codex/config.toml` was not written. The profile's `wakeup.json` used `"target": "pinned"`.

**Run** (two chats, A and B, in the test folder; terminal stand-in initiator, each task approved by
typing `yes`):

- **Wake the bound chat.** A bound itself (revision 1), and t1 was approved and pinned to A.
  - The connector woke A 1.1 s after approval, and Desktop acknowledged in 0.18 s.
  - A started a turn by itself, collected t1 with its own identity, and answered `437`, about
    16 s after approval.
  - B had no turn.
- **Isolation.** B's explicit receive of t1 was refused with `wrong_session` (recorded as an
  incident), and B's general receive returned nothing.
- **Re-binding.** B bound itself (revision 2). t2 was pinned to B, the connector woke B, and B
  answered `493`. A had no turn.
- **Busy chat and re-binding while waiting.**
  - t3 was approved and pinned to B while B was running `sleep 90`. The connector recorded
    `wake_deferred_busy` and sent nothing.
  - A re-bound itself (revision 3) while B was still busy.
  - When B became idle, the connector woke B, not A, about 6 s later, and B answered `713`.
    A had no wake turn.
- Evidence agrees across the profile database (bindings, pins, dispatch stages from intent to
  replied, incidents) and both chat transcripts in Codex's own store.

**Limits:**

- Desktop's thread IPC is undocumented and was exercised on this version only.
- The pinned chat has to be loaded in Desktop. Cold restore (`restore`) and the archived-chat
  refusal were not exercised here.
- Wakes run one at a time per endpoint.
- macOS only; Windows and Linux Desktop IPC transports are unknown.

**Re-check through `setup codex --wake`** (2026-10-10, same chats). Setup's binding replaced the
hand-written one, and the service restarted to load it. Chat A (revision 3) woke by itself and
answered `378`; B had no turn. Desktop started the turn about 6 s after the request, while
resuming a chat left idle overnight, so the bridge's 5 s acknowledgment wait recorded the send as
`sent_unconfirmed`. The connector still observed A collect and answer the task and never resent.
The start-turn wait is now 20 s, inside the connector's 30 s bridge timeout.

## 5. How support is reported

Each row is one **host × OS × mode** combination:

- **Modes:** `attended` or `automatic`.
- **Capabilities:** setup, initiate + approve, bind, exact-chat receipt, wake-up.
- **Evidence:** `automated` (CI job and OS), `real-desktop` (date, host version, OS version,
  checklist result) or `not supported` (reason).

The table lives in `docs/SUPPORT.md` from Increment 2. Until then, claims are limited to the
automated results listed in each increment's acceptance.

## 6. Increments

### Increment 1: portable core, exact-chat rules, isolated setup (automated checks passing)

Smallest set of changes needed to validate the six items below.

1. **OS adapter.**
   - Locks: `fcntl` on POSIX, `msvcrt` on Windows.
   - Private directories and files, checked by owner and DACL on Windows through `pywin32`.
   - Atomic private replacement, detached start and application directories.
   - Move `registry`, `identity`, `cli serve`, `adapter_socket` and the `wakeup.json` check onto
     it.
   - Report `files.py` controlled writes and Unix-socket bridges as **unsupported on Windows**
     until an equivalent race-resistant primitive is verified.
2. **CI** on macOS, Ubuntu and Windows.
   - The full suite runs on macOS. A portable core subset, selected by markers, runs on Ubuntu
     and Windows.
   - A cross-platform startup test covers: setup in a temporary home → service starts → stdio
     MCP lists tools → approve → attended receipt → answer.
3. **Core bindings.**
   - A binding revision for each worker endpoint, and in-chat bind requests whose approval uses
     the trusted session.
   - The target is offered when the task is opened, re-checked and pinned at approval, and
     refused if it changed.
   - Receipt and answers are enforced per session.
   - `connector_delegate` carries the bound chat and revision, so the Claude tool-permission
     prompt shows them.
   - A session-scoped delivery-credential store, kept only in memory, for Claude Windows
     enrollment. It holds only the token; the hook and named-pipe client come in Increment 2.
4. **Installation.**
   - A profile with an installation ID, platform data directory and ownership manifest.
   - All three host endpoints are created once, so later setups never modify a running service.
   - Collision checks on the data directory, MCP entry name, port and running service.
   - `setup <host>` (idempotent, with `--dry-run`), `uninstall` (owned resources only) and
     `doctor`.
   - Host config writers for all three hosts, without granting any tool permissions.
   - The MCP launcher starts the service on demand.

**Acceptance (automated):**

| Validation | Evidence |
| --- | --- |
| Cross-platform startup | Startup test green on the three CI OSes; core subset green on Ubuntu and Windows; full suite green on macOS |
| Approval / rebinding races | Re-bind between open and approval → refused, nothing delivered. Re-bind after approval → the task stays on its pinned chat. A concurrent bind and approval resolve to one consistent outcome. |
| Wrong-session rejection | Another chat on the same endpoint cannot receive or answer a pinned task. Calls without a trusted identity cannot receive pinned tasks. A model-supplied session is ignored. |
| Stale credentials | Re-enrollment, re-binding, unbinding, expiry and restart each invalidate the old credential. Credentials never appear in status, incidents, logs or errors. |
| Repeated setup | A second `setup` changes nothing. Unrelated host entries and comments are preserved. `--dry-run` writes nothing. |
| Installation isolation | A foreign data directory, MCP entry or occupied port is detected and refused. Two profiles coexist. `uninstall` removes only owned entries and leaves edited or foreign ones in place. |

**Results (2026-10-05, branch `increment-1-portable-core`):**

All three platforms passed on the same commit (`b22a62c`, CI run 37389970684):

| Platform | Result | Scope |
| --- | --- | --- |
| macOS (local and CI) | 909 passed | Full suite |
| Ubuntu | 786 passed | macOS host-integration modules are excluded (`tests/conftest.py`). |
| Windows | 530 passed, 2 skipped | Also excludes POSIX-only modules: Unix-socket bridges, Codex Desktop IPC, native-conversation providers, the Codex catalog and `files.py` workspace writes. The 2 skips are symbolic-link cases, which need privileges on Windows. |

Windows problems found and fixed during these runs:

- `fcntl` was imported at module level.
- `OWNER RIGHTS` access entries (used by CPython for `0o700` directories) were treated as other users.
- The detached service crashed on a non-ASCII banner written in the ANSI code page.
- Windows releases locks and file handles of an exited process with a delay.

Coverage of each validation:

| Validation | Tests |
| --- | --- |
| Cross-platform startup | `test_startup_e2e.py` runs on every CI platform. It uses the command lines written by `setup`, starts the service on demand as a real detached process, binds in natural language, approves, delivers to the exact chat and returns the answer. |
| Approval / rebinding races | `test_bindings.py`: re-binding before approval, after approval and concurrently, in both orders |
| Wrong-session rejection | `test_bindings.py` and the e2e test: another chat on the same endpoint, and a call with no identity |
| Stale credentials | `test_bindings.py`: re-enrollment, re-binding back to the same chat, expiry, unbinding, restart, and no exposure in status, incidents, errors or the database |
| Repeated setup | `test_install.py` and the e2e test |
| Installation isolation | `test_install.py`: live-style entry name collision, Claude project-scope shadowing, foreign data directory, port taken by another program, side-by-side profile, owned-only uninstall and purge |

Not verified by Increment 1:

- **Any real desktop.**
- **Claude chat identity on a real host.** Delivery of `CLAUDE_CODE_SESSION_ID` to the stdio MCP process comes from Claude's changelog and is not observed yet.
- **Codex `_meta` with the new per-call identity.** The `_meta` thread ID was observed with the earlier pinned identity mode, not with this one.
- **Windows ACL behaviour beyond the CI runner account**, for example on standard user accounts or managed devices.
- **Windows service lifetime.** A service started on demand inside a launcher that puts its children in a kill-on-close job object stops with that launcher. The MCP SDK's own Windows launcher does this. The next call restarts the service from its stored state. Login autostart (Increment 3) removes the dependency.
- **Two test-only changes.** Two existing MCP round-trip tests had their 3 s waits raised to 15 s after timing out on the Ubuntu runner. Passing runs are not slower.

### Increment 2: automatic wake-up on the pinned target

- The dispatcher reads the pinned target, and `wakeup.json` keeps only legacy static bindings.
  - **Started 2026-10-09:** a binding with `"target": "pinned"` wakes the chat each task was
    pinned to at approval, and the Codex Desktop bridge accepts that chat as
    `{"session": "codex:<thread id>"}`. Validated on a real Codex Desktop on macOS (section 4.5).
  - `setup codex` writes that binding by default on macOS; `--no-wake` turns it off.
- **Claude inbox adapter:**
  - On macOS, Linux and WSL 2 it uses the Unix socket without a token.
  - On Windows it uses the named pipe with a token from an explicitly enrolled session hook.
  - It honours inbound hold and refuse, and `doctor` reports them.
- **Antigravity sidecar adapter:** the project-owned hop moves to loopback HTTP with a per-bridge
  token, and the host inbox still uses `agentapi`.
- **Codex macOS IPC adapter:** on by default (decision 3).
- Every adapter has a host-version contract that refuses unknown versions.
- `docs/SUPPORT.md` is introduced.

### Increment 3: onboarding and documentation

- Binding by name from the initiating chat (Codex app-server `thread/list`).
- Optional autostart: LaunchAgent, systemd user unit, Windows logon task.
- `install.sh` and `install.ps1`.
- Short READMEs in English and Chinese: purpose, environments, one onboarding flow, key limits,
  links.
- Configuration, API reference, limitations and development history move to `docs/`, and
  actionable backlog items move to GitHub issues.

### Increment 4: real-desktop acceptance (macOS → Windows → Linux)

The acceptance environments are chosen before any live host configuration changes. Each
combination is checked per host × OS × mode with this checklist:

1. Install with the host's one flow on a clean account; `doctor` reports ready.
2. Bind in natural language. Decline a re-bind and confirm the old binding is kept.
3. Delegate a task and approve it in the initiating desktop.
4. The exact bound chat receives it, and the real answer returns to the initiating chat.
5. A declined task delivers nothing.
6. Restarting the service, and separately the host app, recovers in-flight work or reports its
   real state.
7. Running setup again changes nothing.
8. Re-binding keeps open tasks on the old chat.

## 7. Open risks

- **Non-public host details.** Each is wrapped in a version contract and refused when unknown:
  - Codex Desktop IPC and its unknown Windows transport.
  - The Claude inbox frame format.
  - Antigravity `get-conversation-metadata` and its internal RPC.
- **Claude Code inbound controls.** A worker session that bypasses permission prompts holds
  external messages, and Desktop drops them after 5 minutes. Setup never changes
  `crossSessionInbound`.
- **Antigravity.**
  - Its 180 s tool deadline affects initiating.
  - Its chat identity relies on an undocumented request-metadata key (section 4.4).
- **Preview hosts on Linux.** The Codex app (preview) and Claude Desktop (beta) are claimed as
  automated-only until stable.
- **Windows ACL semantics.** Inherited ACLs and elevated owners must be verified on real Windows,
  not just in CI.
- **Same-user processes** are not an isolation boundary. Local credentials protect against other
  OS users and browsers, not against malware running as the same user.
