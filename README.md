# Local AI Connector

**English** | [简体中文](README.zh-CN.md)

Local AI Connector lets AI agents in different desktop apps hand tasks to each other through one service on your
own computer. For example, a Claude chat can ask an Antigravity chat to review some code:

- You approve every task in the app that started it.
- The task goes only to the chat you bound for that work.
- The worker's real answer comes back to the chat that asked.

Supported apps: Codex, Claude Desktop (Code tab) and Antigravity, on macOS, Windows and Linux, with the support
levels listed under [Current support](#current-support).

> **Public preview, not production-ready.** A worker chat must currently be asked to collect its tasks; automatic
> wake-up is not implemented yet. Real-desktop verification so far covers Antigravity on macOS only.

## How it works

1. **Install once per app.** `local-ai-connector setup <app>` adds this connector to that app's MCP settings. It
   never grants tool or file permissions; it tells you which ones to allow.
2. **Bind a worker chat.** In the chat that should receive tasks, say *"Use this chat for connector tasks."* The
   app asks you to approve.
   - The app itself identifies the chat; the model cannot choose it.
   - Binding gives no task permission by itself.
   - To switch chats later, bind another one. No reinstall is needed.
3. **Start a task.** In another app's chat, ask for help, for example *"Ask Antigravity to check this function."*
   - You approve the task there.
   - Approval pins the task to the bound chat.
   - Re-binding later never moves an approved task.
4. **Collect and answer.** In the worker chat, say *"Check for connector tasks."* It reads the task and replies.
   Other chats of the same app cannot read or answer it.
5. **Get the result.** The reply returns to the chat that started the task. Follow-up rounds stay on the same chat.

## Installation

Requirements:

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which provides Python 3.13.
- The desktop app you want to connect.
- For Claude, the `claude` command from Claude Code on your PATH. On macOS, the copy bundled with Claude Desktop is
  used when `claude` is not on your PATH.

```sh
git clone https://github.com/wuwaforF/local-ai-connector.git
cd local-ai-connector
uv sync --frozen
uv run local-ai-connector setup antigravity     # or: codex, claude
uv run local-ai-connector doctor
```

After `setup`:

- Restart or reconnect MCP servers in that app, as `setup` tells you.
- Keep the clone where it is. The app's entry points at its Python environment, so run `setup` again after moving
  it.
- `setup` refuses to overwrite an MCP entry, data folder or port it did not create. To install alongside an
  existing setup, use `--profile <name>`.
- `uninstall` removes only what this installation added; `uninstall --purge` also deletes its data.

## Current support

| | macOS | Windows | Linux |
| --- | --- | --- | --- |
| Service, `setup`/`doctor`/`uninstall`, approval and binding rules | Automated tests | Automated tests | Automated tests |
| Antigravity exact-chat binding and isolation | **Verified on a real desktop** (Antigravity 2.19.1) | Automated tests only | Automated tests only |
| Codex and Claude Desktop Code per-chat identity | Automated tests only | Automated tests only | Automated tests only |
| Approval from a real initiating desktop, end to end | Needs acceptance | Needs acceptance | Needs acceptance |
| Automatic wake-up of the worker chat | Not implemented | Not implemented | Not implemented |

What these levels mean:

- **Antigravity on macOS:** exact-chat binding and isolation were verified with Antigravity 2.19.1, using two real
  chats in an isolated test profile.
  - A bound chat received and answered its task.
  - The other chat could not read or answer it.
  - Re-binding kept an already approved task on its original chat.
  - All of this still held after restarting Antigravity.
- **Windows and Linux** have automated test coverage in CI, but no real Antigravity acceptance yet
  ([#3](https://github.com/wuwaforF/local-ai-connector/issues/3)).
- **Task collection is explicit:** a worker chat must be asked to collect its tasks. Automatic wake-up is not
  implemented ([#1](https://github.com/wuwaforF/local-ai-connector/issues/1)).
- **End-to-end approval from a real initiating desktop still needs acceptance**
  ([#2](https://github.com/wuwaforF/local-ai-connector/issues/2)). In the Antigravity test, a person approved each
  task, but the initiating side was a terminal stand-in.
- **Antigravity's conversation metadata key is undocumented.** It is `antigravity.google/conversation_id`, observed
  on 2.19.1. If a version stops sending it, binding fails safely with `missing_session_identity` and never falls
  back to an unverified identity ([#4](https://github.com/wuwaforF/local-ai-connector/issues/4)).
- **Codex and Claude** chat identities are not yet confirmed on real desktops
  ([#5](https://github.com/wuwaforF/local-ai-connector/issues/5)).
- **Not isolated from your own programs:** other programs running as your own OS user can read the connector's
  local data. It is protected from other users, not from processes of the same user.

## Capabilities

**Available now**

- One local service shared by all connected apps, set up per app on macOS, Windows and Linux.
- Separate installation profiles.
- Natural-language binding of a worker chat, with approval in that chat. Re-binding needs no reinstall.
- Approval of every task in the initiating app. The approval pins the target chat and its binding revision.
- Isolation between chats of the same app, real answers, follow-up questions and further rounds on the same task.

**Planned**

- Automatic wake-up of the bound chat ([#1](https://github.com/wuwaforF/local-ai-connector/issues/1)).
- Real-desktop acceptance on all platforms
  ([#2](https://github.com/wuwaforF/local-ai-connector/issues/2),
  [#3](https://github.com/wuwaforF/local-ai-connector/issues/3),
  [#5](https://github.com/wuwaforF/local-ai-connector/issues/5)).
- Login autostart ([#6](https://github.com/wuwaforF/local-ai-connector/issues/6)).
- Installing from a release ([#9](https://github.com/wuwaforF/local-ai-connector/issues/9)).
- English CLI messages ([#8](https://github.com/wuwaforF/local-ai-connector/issues/8)).
- See all [open issues](https://github.com/wuwaforF/local-ai-connector/issues).

## Documentation

- [Reference](docs/REFERENCE.md): tools, configuration, identity sources and manual setup.
- [Architecture](docs/ARCHITECTURE.md): how the code is organised.
- [Platform plan and evidence](docs/PLATFORM_PLAN.md): the support matrix and how each claim was tested.
- [Contributing](CONTRIBUTING.md) and the [security policy](SECURITY.md). Please report vulnerabilities privately.
- [Research tools](research/): reproducible diagnostics, such as the Antigravity chat-identity probe.

## License

MIT. See [LICENSE](LICENSE). Third-party notices are in [NOTICE.md](NOTICE.md).
