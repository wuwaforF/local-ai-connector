# Contributing

Thanks for helping improve Local AI Connector. The project is a public preview. Good issues, reviews and small, well-tested pull requests are all welcome. Start with the [open issues](https://github.com/wuwaforF/local-ai-connector/issues) (look for `help wanted` and `good first issue`), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/PLATFORM_PLAN.md](docs/PLATFORM_PLAN.md).

## Development setup

macOS with [uv](https://docs.astral.sh/uv/) is required (Python 3.13 is what CI and the maintainer use).

```sh
git clone <your fork>
cd local-ai-connector
uv sync --frozen --extra test
uv run pytest -q
```

Some tests bind local TCP ports and Unix sockets. If a sandbox blocks that, run them in a normal terminal. Tests never contact desktop apps or an installed connector.

Use a separate clone for development. Do not develop in a directory that a running connector service or a host's MCP configuration points to: those processes import code from that checkout, so editing it changes a live installation.

## Rules that must not be broken

These are the project's safety contract. A change that weakens any of them will not be merged, even if tests pass.

1. **Approval happens in the initiating desktop.** A new task becomes visible to a worker only after the user approves the exact target and full task text there. Approval comes through an MCP form or the host's per-call tool permission. No tool argument, model output, client name or failure path may grant approval.
2. **Exact-chat routing.** Delivery goes only to the chat registered for the target endpoint. A changed or missing target stops delivery and is never silently re-routed. Follow-up rounds reuse the original channel and approval and never extend its expiry.
3. **No blind retries of side effects.** Intent is recorded before a send. Unknown outcomes are reconciled, not repeated.
4. **Saving a binding is not standing permission.** A saved target sends nothing, and every later task is approved again.
5. **No credentials or private data in the repository.** Endpoint and approval tokens, host MCP settings, SQLite state, logs, real chat, session or project IDs, and home-directory paths all stay out. Use placeholders such as `/absolute/path/to/...` and synthetic IDs in tests, docs and examples.
6. **Local only.** The service binds to `127.0.0.1` and rejects browser origins. Remote access needs a design discussion first.

## Pull requests

- Keep changes focused, and add or update tests for behaviour changes. Approval, routing, expiry and idempotency changes need tests for the refusal paths too.
- Run `uv run pytest -q` before opening the PR. CI runs the same suite on macOS.
- Match the surrounding code style. There is no formatter configuration yet, so do not reformat unrelated code.
- Changes to host bridges (`integrations/`) should say which host version you tested against. Note anything that could only be checked by hand, such as a real desktop approval prompt. Automated tests simulate hosts and do not prove real host behaviour.
- `deployment/` scripts change a real installation. Keep them preview-by-default where possible and parse all arguments before touching any file.

## Documentation language

English is the primary documentation language; Chinese is secondary. Update `README.md` first, then `README.zh-CN.md` if you can (or say in the PR that the Chinese version needs a follow-up). New docs, code comments and MCP tool descriptions should be in English. The dated design notes in `docs/` are historical and mostly Chinese, so treat them as background rather than specification.

## Reporting bugs and security issues

- Open a GitHub issue with steps to reproduce, the host apps and versions, and relevant `connector_status` or `wakeup-status` output. Redact IDs and paths.
- Do **not** report security problems in public issues; follow [SECURITY.md](SECURITY.md).

---

## 中文摘要

欢迎参与改进。

**开发环境**
- 在 macOS 上安装 uv 后，执行 `uv sync --frozen --extra test` 和 `uv run pytest -q`。
- 请在独立的克隆目录开发，不要修改正在运行的连接器服务或宿主 MCP 配置所指向的目录。

**不能破坏的约束**
- 新任务必须在发起端 Desktop 由用户批准。
- 只投递到登记的准确聊天，不静默改投。
- 结果未知的副作用不能盲目重试。
- 保存绑定不等于长期授权。
- 仓库中不得出现凭据、真实 ID 或本机路径。
- 服务只监听本机。

**提交 PR**
- 行为改动须附带测试，拒绝路径也要覆盖。
- 宿主桥接改动请注明测试的宿主版本。

**文档语言**
- 以英文为主、中文为辅，优先更新 `README.md`。

**安全问题**
- 请按 [SECURITY.md](SECURITY.md) 私下报告，不要公开提 issue。
