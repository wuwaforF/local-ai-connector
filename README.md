# Local AI Connector

**English** | [简体中文](README.zh-CN.md)

Local AI Connector lets AI agents in different desktop apps hand tasks to each other through one service on your
own computer. For example, a Claude chat can ask an Antigravity chat to review some code:

- You approve every task in the app that started it.
- The task goes only to the chat you bound for that work.
- The worker's real answer comes back to the chat that asked.

Supported apps: Codex, Claude Desktop (Code tab) and Antigravity, on macOS, Windows and Linux, with the support
levels listed under [Current support](#current-support).

> **Public preview, not production-ready.** With the setup flow described here, a bound worker chat collects its
> tasks when asked. Waking that chat automatically still needs the existing wake adapters to be connected to this flow
> and re-tested. Real-desktop verification of this flow so far covers Antigravity on macOS.
> An [earlier macOS deployment](#earlier-macos-deployment) used a different setup and is described separately.

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

This table covers the setup flow documented above (`setup` and natural-language binding).

| | macOS | Windows | Linux |
| --- | --- | --- | --- |
| Service, `setup`/`doctor`/`uninstall`, approval and binding rules | Automated tests | Automated tests | Automated tests |
| Antigravity exact-chat binding and isolation | **Verified on a real desktop** (Antigravity 2.19.1) | Automated tests only | Automated tests only |
| Codex and Claude Desktop Code per-chat identity | Automated tests only | Automated tests only | Automated tests only |
| Approval in a real initiating desktop | Needs acceptance with this flow | Needs acceptance | Needs acceptance |
| Automatic wake-up of the bound chat | Not yet connected to this flow | Not yet connected; no real-host test | Not yet connected; no real-host test |

What these levels mean:

- **Antigravity on macOS:** exact-chat binding and isolation were verified with Antigravity 2.19.1, using two real
  chats in an isolated test profile.
  - A bound chat received and answered its task.
  - The other chat could not read or answer it.
  - Re-binding kept an already approved task on its original chat.
  - All of this still held after restarting Antigravity.
- **Windows and Linux** have automated test coverage in CI, but no real Antigravity acceptance yet
  ([#3](https://github.com/wuwaforF/local-ai-connector/issues/3)).
- **Wake-up:** with this flow, a bound chat collects its tasks when asked. Wake adapters exist for Codex Desktop,
  Claude Desktop Code and Antigravity, and they worked in the earlier macOS deployment. There they woke a fixed,
  preconfigured chat.
  - The remaining work is to deliver to the chat pinned when a task is approved, then re-test on real desktops.
  - Windows and Linux real-host acceptance is still outstanding.
  - Tracked in [#1](https://github.com/wuwaforF/local-ai-connector/issues/1).
- **Approval in a real initiating desktop:** this was accepted in the earlier macOS deployment, but not yet with this
  flow ([#2](https://github.com/wuwaforF/local-ai-connector/issues/2)). In the Antigravity two-chat test, a person
  approved each task, but the initiating side was a terminal stand-in.
- **Antigravity's conversation metadata key is undocumented.** It is `antigravity.google/conversation_id`, observed
  on 2.19.1. If a version stops sending it, binding fails safely with `missing_session_identity` and never falls
  back to an unverified identity ([#4](https://github.com/wuwaforF/local-ai-connector/issues/4)).
- **Codex and Claude** chat identities are not yet confirmed on real desktops
  ([#5](https://github.com/wuwaforF/local-ai-connector/issues/5)).
- **Not isolated from your own programs:** other programs running as your own OS user can read the connector's
  local data. It is protected from other users, not from processes of the same user.

## Earlier macOS deployment

Before `setup` existed, a macOS deployment wired each worker to a preconfigured chat, using maintainer scripts and
manual configuration. Real-desktop runs on that deployment showed:

- **Codex → Antigravity:** automatic wake-up, a real answer, and a follow-up continuation.
- **Codex → Claude Desktop Code:** automatic wake-up and a real answer.
- **Claude → Antigravity:** automatic wake-up and a real answer. In that run, approval was confirmed in Codex; a later
  run confirmed approval with a single click in Claude.

These results apply to that deployment only. A fresh installation made with `setup` does not provide them yet.

- The manual setup is described in the [Reference](docs/REFERENCE.md).
- The dated acceptance records are in [docs/PROJECT_NOTES.md](docs/PROJECT_NOTES.md), in Chinese. Their evidence
  files are kept by the maintainer and are not published.

## Capabilities

**Available now**

- One local service shared by all connected apps, set up per app on macOS, Windows and Linux.
- Separate installation profiles.
- Natural-language binding of a worker chat, with approval in that chat. Re-binding needs no reinstall.
- Approval of every task in the initiating app. The approval pins the target chat and its binding revision.
- Isolation between chats of the same app, real answers, follow-up questions and further rounds on the same task.

**Planned**

- Wake-up for this flow: connect the existing adapters to the chat pinned at approval and re-test them on all
  platforms ([#1](https://github.com/wuwaforF/local-ai-connector/issues/1)).
- Real-desktop acceptance of this flow on all platforms
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
