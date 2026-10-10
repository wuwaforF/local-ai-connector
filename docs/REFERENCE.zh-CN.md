# 参考说明

[English](REFERENCE.md) | **简体中文**

本文是 [README](../README.zh-CN.md) 之外的详细中文参考，包括手动（开发者）配置、MCP 工具、身份模式、唤醒适配器和原生聊天续接。
在桌面应用中安装请使用 README 介绍的 `local-ai-connector setup <codex|claude|antigravity>`。本文记录的是手动配置方式，
如与英文 [REFERENCE.md](REFERENCE.md) 不一致，以英文为准。平台支持情况与证据见 [PLATFORM_PLAN.md](PLATFORM_PLAN.md)。

## 安装与启动

当前验证环境为 macOS Apple Silicon、Python 3.13。需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)；图形窗口首次编译需要 macOS Command Line Tools。无图形环境可以使用终端批准入口。Windows 尚不支持当前的文件锁实现。

在源码目录执行：

```sh
uv sync --frozen --extra test
uv run local-ai-connector --data .connector init --peers writer reviewer tester
uv run local-ai-connector --data .connector serve
```

保持服务运行。使用 peer 模式批准任务或查看记录时，可在另一个终端打开控制窗口：

```sh
uv run local-ai-connector --data .connector ui
```

窗口显示求助原文、连接状态、批准／拒绝／撤销按钮，以及模型服务地址、模型名称、可选密钥和连接测试。中文采用黑色宋体。也可直接使用：

```sh
uv run local-ai-connector --data .connector status
uv run local-ai-connector --data .connector approve
```

默认仅监听 `127.0.0.1:38471`。`--peers` 接受两个或更多独立名称；省略时创建 `worker-a`、`worker-b`。每个端点有独立凭据，名称不能为 `server` 或 `model`。可用其他数据目录及 `--port 38472` 运行独立服务。数据目录含凭据和交流内容，已从 Git 排除。

### 登记工作者及能力

停止服务后，可添加任意名称的端点：

```sh
uv run local-ai-connector --data .connector peer-add proofreader \
  --name 'Proof reader' --description '检查事实与措辞' \
  --capability fact-check --capability proofreading
```

启动服务后，`connector_status` 返回 `workers` 能力目录；`id` 用作委派目标，`self` 表示调用方端点。能力来自管理员声明；`availability: unknown` 不代表在线。登记命令拒绝覆盖已有凭据，并拒绝在服务运行时修改登记文件。

登记时可加 `--mcp-locale en-US`，为该 stdio 端点使用美式英文指令和工具说明；默认 `zh-CN`。该设置不改变工具参数、消息内容或宿主系统提示，共享 HTTP MCP 保持原有语言。

独立启动仍使用 `serve`。macOS 可用 `service-config` 导出 launchd 配置；安装与加载属于系统部署步骤，命令本身不会修改系统设置。维护者环境中已由当前用户的 LaunchAgent 托管，终止后自动恢复、现有 MCP 重连和历史记录保留均通过实测，启动不再依赖 Codex；重新登录／重启电脑后的自动加载尚未实测。见 后台服务验证记录（`deployment/service-autostart-verification-20260921.json`，维护者本机记录，未随源码发布）。

## 通用 MCP 接入

### stdio

为各任务生成对应端点配置：

```sh
uv run local-ai-connector --data .connector client-config --peer writer
uv run local-ai-connector --data .connector client-config --peer reviewer
uv run local-ai-connector --data .connector client-config --peer tester
```

默认输出包含 `mcpServers` 的 JSON，服务器条目中有 `command`、`args`。将对应条目合并到宿主的 MCP 配置中；不同宿主的外层结构可能不同，例如 VS Code 使用 `servers`。生成内容包含 Python 和端点文件的绝对路径，更换安装目录或环境后需重新生成。

保留两个已有宿主的输出格式：

```sh
uv run local-ai-connector --data .connector client-config --peer writer --client codex
uv run local-ai-connector --data .connector client-config --peer reviewer --client zcode
```

`--client` 只选择配置格式，不改变端点身份模式，也不会自动编辑宿主配置。Codex 输出 TOML；ZCode 输出 `mcp.servers`。默认生成的工具超时为 60 秒，配合连接器默认 20 秒等待。需要显式长等待时，应相应提高宿主超时。历史桌面加载与长等待实测见 [验证记录](VALIDATION.md)。

### 聊天内委派（stdio requester）

请求方可使用 `requester` 工具配置，提供 `connector_status`、`connector_delegate`、`connector_continue` 和 `connector_archive` 四个工具。`delegate` 通过 MCP form elicitation 在请求方聊天中展示目标与任务原文；用户确认后，程序投递任务并等待回复。`continue` 在原授权有效期内继续同一任务。宿主必须支持表单确认；能力缺失时明确报错。工作者 peer 模式与 HTTP 入口提供下文列出的六个工具。

停止服务，将请求端点文件（例如 `.connector/writer.json`）补充为以下结构，保留已有端点身份和凭据。以下所有尖括号内容均为占位符：

```json
{
  "url": "http://127.0.0.1:38471",
  "token": "<已有 writer 端点凭据>",
  "peer": "writer",
  "client": "generic",
  "tool_profile": "requester",
  "approval_token": "<独立随机审批凭据>"
}
```

在 `.connector/server.json` 中合并以下字段；映射键必须是已登记的请求端点，值须与该端点文件中的 `approval_token` 一致，并区别于管理员、通信端点和其他审批凭据：

```json
{
  "approval_tokens": {
    "writer": "<同一个独立随机审批凭据>"
  }
}
```

配置文件仅供本机服务和 stdio 进程读取，应保存为仅所有者可读写。重新启动服务后，在 Codex MCP 配置中合并下面的条目，替换绝对路径；不要将凭据放入提示词或工具参数：

```toml
[mcp_servers.local_ai_connector]
command = "/absolute/path/to/local-ai-connector/.venv/bin/python"
args = ["-m", "local_ai_connector.cli", "mcp", "--config", "/absolute/path/to/local-ai-connector/.connector/writer.json"]
startup_timeout_sec = 20
tool_timeout_sec = 660
enabled_tools = ["connector_status", "connector_delegate", "connector_continue", "connector_archive"]

[mcp_servers.local_ai_connector.tools.connector_status]
approval_mode = "approve"

[mcp_servers.local_ai_connector.tools.connector_delegate]
approval_mode = "approve"

[mcp_servers.local_ai_connector.tools.connector_continue]
approval_mode = "approve"

[mcp_servers.local_ai_connector.tools.connector_archive]
approval_mode = "approve"
```

`approval_mode = "approve"` 准许这四个工具入口；新任务通信与归档分别由各自的表单确认，工具入口许可不等于任务批准。让 Codex 重新加载 MCP 配置，并确认当前任务的工具列表已更新。其他宿主需要按其配置格式接入，表单显示及等待行为需单独验证。

调用约定：

- 先用 `connector_status` 查找 `workers`，再以目标 `id` 调用 `connector_delegate(target, message, request_key, conversation_mode)`。目标及任务原文出现在确认表单中；接受并确认后投递，拒绝或取消则返回实际决定，任务不投递。
- 执行等待默认 180 秒，可设 `timeout_seconds=1..600`，从确认后计时。返回 `running`、`approval_timeout` 或工具等待中断时，复用原 `request_key`、`target`、`message`、`conversation_mode` 恢复同一任务。确认等待上限为 300 秒；任务授权总期限为 1 小时。
- 返回 `input_required` 时，将 `questions` 中对应问题的 `id` 作为 `reply_to`、补充内容作为 `reply`，连同原任务参数再次调用。补充回复重试也保持这两个值不变。原任务尚未完成时，未读取的补充会通过已配置适配器唤醒工作者继续执行；等待用户补充的时间不计入工作者执行超时，任务授权期限仍然有效。
- 原任务的下一轮使用 `connector_continue(channel, message, request_key)`；工作者追问时，在相同参数上附加 `reply_to`、`reply`。重试须保持 channel、正文和编号完全一致。空闲关闭的通道只在原授权期限内恢复，授权不会续期；过期、撤销或拒绝的通道不能重开。其他新任务仍走 `connector_delegate` 并重新确认。
- `completed` 的 `answer.body` 是工作者实际回复；到期、拒绝及错误按返回状态处理。两端的 MCP 收发和连接器通道记录可用于核对结果。

任务空闲关闭后，在原授权期限内仍可用原参数读取答案；超过期限返回 `expired`，本地归档保留。升级源码后需重启独立服务，并让宿主重新连接 stdio MCP，确保常驻进程加载新实现。

### 双向工作者与聊天内批准（stdio participant）

同一端点既需要委派任务，也需要接收任务时，使用 `participant` 配置。它暴露七个工具：`connector_status`、`connector_delegate`、`connector_continue`、`connector_archive`、`connector_receive`、`connector_send`、`connector_finish`。发起任务时，delegate 在本宿主聊天内请求批准并等待实际答案；收到来信时，receive/send 保持原有工作者流程。`requester` 也提供 `connector_continue`；`peer` 与 Streamable HTTP 工具集包括它。

停止服务后，为已登记的工作者启用：

```sh
uv run local-ai-connector --data /absolute/path/to/data enable-chat-approval writer reviewer --mcp-locale en-US
```

此命令为选定端点生成独立审批凭据，切换为 participant，并保持原有通信身份与其他配置。重复执行保留已有匹配凭据；运行中服务、配置冲突或不匹配身份会拒绝更新。重新启动服务并在宿主重新加载 MCP 后生效。

聊天确认采用标准 MCP form elicitation，宿主必须支持实际表单交互。批准只适用于本端点发起的通道；模型不能通过参数自行批准。英文模式覆盖批准正文、表单说明、进度和结果提示，任务与答案原文保持不变。宿主能力未声明时，委派在创建通道前失败；真实 UI 是否正常展示仍需逐宿主验收。

本机 Claude 和 Gemini 已通过 [enable-peer-chat-approval.command](../deployment/enable-peer-chat-approval.command) 升级并加载新工具。共享确认表单现已取消 Confirmed 复选框：Codex → Claude 返回 35，用户确认直接批准即可；Antigravity → Claude 返回 221，直接提交表单后自动完成。Claude Desktop Code 发起侧仍立即返回 decline，未观察到任务表单、未投递消息，尚未通过聊天内批准验收（后续改用宿主逐次工具许可，见项目笔记）。详细证据见 [项目笔记](PROJECT_NOTES.md)。

此表单批准的是本次任务通信。工作者执行文件修改、命令等操作仍按其宿主权限处理。空闲工作者能否开始执行取决于已配置并验证的唤醒适配器；端点登记本身不代表工作者在线。

### Antigravity 接收方的通信权限

已在 macOS 的 Antigravity 2.15.0 专用测试项目验收。打开 **Settings → Projects → 目标项目 → MCP Tools**，分别添加两条 **Allow**：

```text
local_ai_connector/connector_receive
local_ai_connector/connector_send
```

这里的 `local_ai_connector` 必须与该宿主实际登记的 MCP 服务名一致。这两条项目规则让工作者自动收取已获连接器批准的任务、回传关联结果；每个新任务的确认仍在 Codex 表单完成。任务中的文件、命令等操作继续使用 Antigravity 自身的权限规则。[官方权限说明](https://antigravity.google/docs/permissions)支持按 `mcp(server/tool)` 精确授权；其他项目需要分别设置。

本机复测已由用户确认只在 Codex 批准一次，批准后约 11 秒收到 Gemini 回复，双方记录一致。完整证据见 [通用化及委派报告](GENERALIZATION.md)。

### Streamable HTTP

同一个 `serve` 进程同时提供标准 MCP 地址 `http://127.0.0.1:38471/mcp`。生成某个端点的连接参数：

```sh
uv run local-ai-connector --data .connector client-config --peer reviewer --transport streamable-http
```

输出的 `url` 与 `headers.Authorization` 用于宿主的远程 MCP 配置；也可添加 `--client codex` 生成对应 TOML。ZCode 的 HTTP 外层配置本轮未验证，使用通用输出按其界面填写。HTTP 导出包含该端点的凭据，应保存到宿主的私有设置中。

HTTP 每个请求通过 Bearer 凭据识别端点；管理员凭据不能调用 `/mcp`。服务只绑定本机地址，拒绝浏览器 Origin 请求及非许可 Host。此实现面向本机可信客户端，未提供公网部署、OAuth 注册或云端连接能力。`/call` 仍是内部诊断接口，MCP 客户端应连接 `/mcp`。

HTTP 使用 SDK 的无状态传输模式，消息及授权状态保存在 SQLite。断线后重新连接，带原游标继续接收；发送重试复用原去重编号。一个端点可以从 stdio 切换到 HTTP，身份和消息仍属于同一端点。

## 身份模式与已有配置

默认 `generic` 模式按凭据隔离端点，不依赖宿主专有任务 ID。进程重启不会产生新身份。同一端点凭据被两个任务使用时，它们会共享收件箱和通道；要隔离任务，必须分别分配端点。通道目前仍是两个端点之间的双向交流，多端点意味着可创建多条双向通道。

Codex／ZCode 的原生任务绑定为可选模式，创建时显式指定：

```sh
uv run local-ai-connector --data .native-connector init --port 38472 \
  --peers codex-review zcode-review \
  --native-peer codex-review=codex --native-peer zcode-review=zcode
```

原生模式只使用 stdio，从宿主元数据读取任务 ID，并保留首次绑定、缺失身份拒绝及跨任务冲突检查。HTTP 入口拒绝这些端点，避免意外把原生绑定当成通用凭据模式。端点模式在服务启动时加载。

已有 `client: codex`／`client: zcode` 配置及绑定保持有效，无需修改。已有 `generic`（或省略 `client`）配置按凭据恢复，旧版本写入的 `generic:随机UUID` 绑定不再参与身份判定；配置文件本身不会被重写。配置、数据库与凭据要一起保留。需要从原生模式切换到通用模式时，推荐用新数据目录创建独立端点。

其他宿主若提供可信的任务元数据，可通过配置声明读取路径：

```sh
uv run local-ai-connector --data .connector peer-add another-worker \
  --session-namespace my-host --session-path vendor/context conversation id
```

这会使用 `metadata` 身份模式，从 MCP 请求元数据的嵌套字段读取 ID。路径必须符合该宿主实际发送的结构；缺失字段会明确失败。该模式同样限于 stdio。未提供原生任务元数据的宿主使用 `generic`。

## peer 模式的双向交流

默认 stdio peer 配置和 Streamable HTTP 提供 `connector_status`、`connector_request_help`、`connector_continue`、`connector_receive`、`connector_send`、`connector_finish` 六个工具，使用以下流程：

1. 接收方在选定的桌面任务调用 `connector_receive(channel=null, after=0, timeout=20)`，程序等待事件。
2. 请求方调用 `connector_request_help(target, message, request_key, conversation_mode)`，得到通道编号。
3. 用户在控制窗口阅读原始求助并批准一次，原文才对接收方可见。
4. 请求方通过 `connector_receive(channel=编号, after=0, timeout=20)` 等待回复或追问。
5. `connector_send(kind="answer", reply_to=问题id, ...)` 回答具体问题。追问使用 `kind="question"` 并关联原问题。
6. 所有问题回答完毕后调用 `connector_finish`。默认空闲 120 秒关闭；未完成的问题阻止空闲计时，授权总期限仍然有效。

对已批准任务继续提问时，改用 `connector_continue`，在同一通道内按轮次等待对应答案；它会保留原授权截止时间。唤醒分发会把通道与各接收端配置的目标分别持久绑定，配置目标发生变化时会停止发送，不能静默转到新会话。此绑定仅用于分发完整性；generic 端点没有宿主原生会话身份验证。新建原生聊天采用下述注册能力，不能由新通道编号推断。

每次读取保存返回的 `cursor`，下一次作为 `after`。同一发送操作的重试复用原去重编号；内容变化必须使用新编号。回复错配、越权、重复编号冲突和已关闭通道都会明确报错。取消等待不会删除消息，重连后可以从原游标恢复。

端点“已登记”不等于 AI 在线。接收方可以主动调用 MCP 等待；已配置唤醒适配器时，服务也可在批准后请求宿主启动接收。适配器默认关闭，需要明确目标与配置；详见 [适配器契约](GENERALIZATION.md)。等待在程序内部完成；授权到期、拒绝或撤销会返回明确状态。

`wakeup.json` 中绑定的 `target` 可以是维护者写定的对象（固定一个聊天），也可以是 `"pinned"`。使用 `"pinned"` 时，每个任务唤醒的是批准时固定的那个聊天，以 `{"session": "<命名空间>:<编号>"}` 传给桥接程序：

- 端点必须有可信的聊天身份（`chat_identity`），否则唤醒会被停用并记录 `wakeup_config_error`。
- 重新绑定不会改变已批准任务的去向：待重试的唤醒仍发往该任务固定的聊天。
- 没有固定聊天的任务不会被唤醒（`wake_target_unpinned`）。
- 同一端点一次只进行一个唤醒，即使任务固定在不同聊天。
- 目前只有 Codex Desktop 桥接程序支持固定目标（`{"session": "codex:<线程编号>"}`）。它只按线程编号核对身份，并拒绝已归档的线程（`wake_host_archived`）。
- 在 macOS 上运行 `setup codex --wake`（可选的实验功能）会自动写入这项配置，并重启正在运行的服务使之生效；`--no-wake` 可再次移除。安装程序只管理自己写入的这一项：不会覆盖不是它写入或已被修改的配置，`uninstall` 和 `--no-wake` 也只移除它自己的那一项。

## 文件协作

代码并行修改优先使用 Git 自带的独立 worktree。连接器不接管工作者的成果生产。

```sh
git rev-parse HEAD
git worktree add --detach ../codex-work HEAD
git worktree add --detach ../zcode-work HEAD
```

把两个路径分别交给对应工作者。交接时提供基线提交、工作提交、变更文件及验证结果；先检查 `git status --short`，将应交接的新文件纳入提交。用 `git diff --binary BASE..WORK_COMMIT` 生成完整变更，再在目标副本执行 `git apply --check` 或检查相应提交。实际冲突由工作者整合，检查失败不会自动覆盖文件。

确需通过同一受控接口更新文件时：

```sh
uv run local-ai-connector file-version --root /path/to/workspace shared.txt
uv run local-ai-connector file-write --root /path/to/workspace \
  --source /path/to/new-content.txt --expected 上一步返回的摘要 shared.txt
```

新文件的 `--expected` 填 `missing`。该写入路径在真正修改前取得进程间锁，核对版本，以临时文件原子替换；拒绝路径越界、符号链接、硬链接及特殊文件。当前大小上限 10 MiB。

**范围：锁只约束连接器的受控写入。** 普通编辑器、终端和客户端原生写入不会自动遵守该锁；版本检查也无法封闭外部写入在最终检查与替换之间的竞态。因此不把共享目录视作强制隔离边界，优先使用 worktree 或独立草稿。

## 监管模型

在窗口中填写兼容 Chat Completions 的地址、模型和可选密钥，点击“测试连接”。也可运行：

```sh
uv run local-ai-connector --data .connector model-config
uv run local-ai-connector --data .connector model-test
uv run local-ai-connector --data .connector model-explain 异常编号
```

窗口的“解释最近异常”读取程序记录的异常，再请求所配置的模型。模型只返回中文解释和有限类别的建议，无法批准连接、修改状态或写文件。云端地址须使用 HTTPS，原始接口错误正文及密钥不进入测试报告。启用“关闭模型思考”要求服务支持 `reasoning_effort="none"`。

已验证接口为本机 Ollama 0.34.0 的 `/v1/chat/completions`，包括 JSON mode。其他兼容服务提供配置入口，尚未逐家实测。模型权重由推理服务独立管理，不包含在主体源码或安装包中。

Qwen3 1.7B 已做中文异常实测，但授权到期建议仍不稳定，本版**不设默认监管模型**。详细输出见 [样本结果](model-validation.json)。可用以下命令复测自己的模型：

```sh
uv run python examples/evaluate_supervisor.py --data .connector --output model-results.json
```

## 测试与维护

```sh
uv run pytest -q
```

测试使用临时目录与临时监听端口，不依赖桌面账户或本机已安装的连接器数据。部分测试需要绑定本机端口和 Unix socket。覆盖授权可见性、身份绑定、消息关联、幂等、追问、撤销、取消、到期、重启恢复、真实 stdio 与 Streamable HTTP MCP、跨传输收发、多端点认证与隔离、两个进程竞争写入、路径边界、独立 worktree 及冲突交接。

`.connector` 中保存 SQLite 状态和凭据。停止服务后可备份整个目录；已有授权按原截止时间恢复。不要同时运行两个服务进程使用同一目录。停止测试后通过客户端正式入口移除临时 MCP 配置，再停止服务。不要删除仍需追问的通道数据。

源码采用 MIT 许可证。研究来源与依赖许可证见 [NOTICE](../NOTICE.md)。

## 仓库内容、示例配置与本机数据

| 目录 | 内容 |
| --- | --- |
| `src/local_ai_connector` | 发布的 Python 包：服务、MCP、委派、唤醒派发、文件和模型辅助 |
| `integrations/` | 宿主桥接与部署辅助（Codex Desktop、Claude Desktop Code、Antigravity），从源码目录运行，不打包进 wheel |
| `deployment/` | 维护用的 macOS 终端脚本。部分脚本默认只预览、加 `--apply` 才写入，其余直接执行；运行前先阅读脚本开头的说明 |
| `examples/configs/` | 唤醒适配器、原生会话注册、stdio 启动项和 Antigravity Sidecar 的占位示例，见 [说明](../examples/configs/README.md) |
| `research/` | 独立的表单与工具许可诊断探针，不导入连接器 |

`integrations/` 与 `deployment/` 中的辅助脚本目前假定：通过 `uv sync` 在源码目录建立 `.venv`；数据目录为 `~/.local/share/local-ai-connector`；LaunchAgent 标签为 `dev.local-ai-connector.service`；端点名称为 `gpt`（Codex 发起端）、`codex_desktop`、`gemini`（Antigravity）和 `claude_code`。核心服务本身接受任意端点名称和数据目录。涉及具体聊天或项目的脚本须显式传入目标，例如：

```sh
deployment/register-codex-desktop.command --thread-id <Codex 聊天 ID> --workspace /absolute/path/to/workspace
deployment/enable-codex-desktop-cold-restore.command --thread-id <Codex 聊天 ID> --workspace /absolute/path/to/workspace
deployment/migrate-codex-communication-permissions.command --worker-project /absolute/path/to/workspace --antigravity-project-id <项目 ID>
```

端点凭据、审批凭据、宿主 MCP 配置、SQLite 状态和日志只保存在数据目录及各宿主的私有设置中，不应提交到仓库。文档中提到的带日期验证记录（`deployment/*.json`、`*.jsonl` 等）含本机路径和会话标识，仅保存在维护者本机，已由 `.gitignore` 排除。


## 原生聊天与通道续接

所有发起端共用同一通道、批准、去重与追问实现。`connector_delegate` 和 `connector_request_help` 必须明确提供 `conversation_mode`：

| 用户意图 | 调用 | 结果 |
| --- | --- | --- |
| 交给既有工作者处理新任务 | `delegate(..., conversation_mode="existing")` | 新通道，使用已登记聊天 |
| 明确要求另开聊天 | `delegate(..., conversation_mode="new")` | 仅支持该能力的目标可接收；批准后创建原生聊天 |
| 同一任务追问 | `continue(channel=原通道, ...)` | 复用原批准和原生聊天，不重新 delegate |

`completed` 表示当前轮次已回答。结果的 `next_round` 指向原通道的 `connector_continue`；闲置关闭不会延长原授权，过期或撤销仍不可续接。`conversation.created=true` 和 `conversation.thread_id` 才证明新建了原生聊天。新版 MCP 参数必须在各应用重新加载工具列表后使用；底层旧 HTTP open 默认保持 existing。

`connector_status.workers[].conversation_modes` 来自已配置的提供者注册表。未注册新建提供者的端点对 new 返回 `new_conversation_unsupported`，在批准及投递前失败。目前提供 `codex_ingress` 和 `antigravity_sidecar`；未启用对应提供者时仍只支持既有聊天。Claude 当前仅使用既有聊天；[Claude Desktop Code 会话绑定说明](CLAUDE_ONBOARDING.md)记录了可选的 Desktop 聊天内会话核对工具、终端预览流程及原生授权限制；新工具已按实际启动配置验证，Codex 重开后的原生会话列表调用通过；当前绑定的原生只读核对也已通过，Claude 侧加载仍待验收。

模型和 agent 的终端绑定流程也由 MCP 使用说明提供。启用本机绑定配置后，可读取英文资源 `connector://claude-binding-guide`；资源不可读时，会话列表返回同一指南的本机文件位置。有有效选定会话回执、终端权限和明确授权的 agent 可自行执行已有注册脚本；缺失条件须报告，指南不会自动取得权限。详见 [MCP 绑定指引](CLAUDE_ONBOARDING.md#mcp-提供给模型和-agent-的绑定指引)。

所有者也可用连接器工具管理显式的构建→审阅流程；每个阶段单独批准。接口、回执 schema 和限制见[工作流说明](WORKFLOWS.md)。

Codex 注册位于 `server.json` 的 `conversations.<端点>`，包含 `provider=codex_ingress`、入口 `session_id`、绝对 `cwd`，以及可选的明确模型设置。共享协议不按端点名称分支。提供者只生成原生调用；批准、消息、幂等性、存储和续接共用公共层。

Codex 入口收到获批任务后调用精确的原生 create_thread，等待真实结果并沿原通道回传。PermissionRequest 钩子核对入口、完整参数、有效期和一次性执行记录；PostToolUse 钩子读取原生结果绑定新聊天 ID。新聊天沿用原生宿主权限，不共享入口的连接器凭据。后续轮次和澄清补答通过同一入口发送到已绑定聊天。创建或发送结果未知时不重复执行；缺少回执会阻止入口代答冒充成功。

部署当前已登记的 Codex 入口：先运行 `deployment/enable-native-conversations.command` 预览，再加 `--apply`。它备份三份配置、注册提供者、将已知实验钩子替换为正式钩子，并将入口项目的 create_thread/send_message_to_thread 设为 prompt，由精确授权钩子处理。其他显式冲突配置会拒绝修改。然后在 Codex 中审核信任更新后的入口项目钩子，完全重开 Codex，并重新加载各发起应用的 MCP 工具。真人验收由用户发起。

范围：Codex 当前只支持创建 projectless 聊天；未指定模型时使用宿主默认值。原生创建、回执钩子及追问的组合仍需本机真人验收。PermissionRequest 已消费到原生调用之间仍有撤销竞态；已发出的原生操作不能被连接器原子撤回。同用户进程可访问本机状态库，文件权限不构成对恶意同 UID 进程的隔离。

Codex 快速绑定候选支持先保存协作目标：在发起端按用户提供的名称查找唯一聊天，用 `connector_codex_bind_chat` 在该 Desktop 批准并保存准确的聊天 ID 和工作区。保存按发起端与已登记入口分别记录，持久保留但不发送任务或授予后续任务权限。每个新任务先用 `connector_codex_bound_chat` 读取目标，再用 `connector_codex_quick_bind` 将目标与任务原文交由发起端批准；连接器保留现有可信入口，再向选定聊天投递原生消息。无需给目标聊天安装 MCP 或搬移共享入口。候选默认不启用，配置启用与真人选定聊天、原生审批和实际续聊验收分别记录；首次安装或钩子信任与后续文件/命令权限须按宿主管理。英文单一指南见 [Codex 快速绑定候选](../src/local_ai_connector/codex_binding_guide.md)，启用候选的 MCP 可读取 `connector://codex-binding-guide`。


### Antigravity 原生新建与归档

本机真人流程已通过用户验收：新建真实聊天计算 47 × 19 得 893，沿同一通道及同一原生 ID 加 7 得 900，最后由用户明确要求并单独批准归档。宿主归档回执和活动注册清理已只读核对；证据见 真人验收记录（`deployment/antigravity-native-live-20260930.json`，维护者本机记录，未随源码发布）。归档后续接拒绝仍由自动回归覆盖，本轮未额外发起真人探针。

`antigravity_sidecar` 使用宿主管理的独立 Sidecar。注册包含 `provider`、`project_id`、`workspace_uri` 和私有 `socket` 路径。创建通过官方 `agentapi new-conversation` 执行，验证真实 ID 与项目/工作区后登记；新聊天从明确的 `channel` 领取原始任务，后续轮次发送到同一个 ID。固定工作聊天的无通道收件不会领取这些新聊天任务。端点仍是 generic：这是路由隔离，不是对持有同一端点凭据的恶意客户端的原生身份隔离。

仅当用户在发起端明确要求归档时，调用 `connector_archive(channel=原任务通道, conversation_id=真实对话ID, request_key=稳定编号)`。其批准与原任务批准分开；只有原发起端可请求，目标必须有本连接器的创建回执。正在处理的任务先完成，宿主非空闲时等待。拒绝/取消、完成任务及空闲到期都不归档。原任务授权到期后仍可另行批准清理该已创建聊天。Claude 的宿主工具许可配置对 archive 同样使用强制用户交互标记。

归档获批后，原聊天暂停新投递。宿主返回并读回 `archived=true` 后，删除该聊天的活动注册、原生投递记录及收件去重项；保留通道、批准记录和最小归档结果，阻止旧编号重放或重新创建。归档失败保留登记，结果未知保持暂停且不自动重试。共享端点注册与预先绑定的已有聊天不作为清理对象。`connector_status` 返回新聊天 ID、状态，以及支持归档的工作者 `cleanup_actions=["archive"]`。

归档使用当前 Antigravity 内部 `UpdateConversationAnnotations` RPC，合并 `archived=true` 后再次查询状态，不修改项目文件。该接口已在本机隔离验证，但不具备公开跨版本兼容保证。删除能力未开放。创建、发送或归档结果未知时保留记录供人工核查，不盲目重试；本版没有自动修复未知结果的管理命令。

部署：运行 `deployment/enable-antigravity-native-conversations.command` 预览，确认后加 `--apply`。脚本只更新服务注册、独立 Sidecar、Codex 归档工具入口及 Antigravity 测试项目的精确工具许可；保留已有固定目标和其他配置。部署会备份、等待任务结束、重启服务，并只读确认 Sidecar 的项目/工作区；失败恢复原配置和服务。部署期间不要发起新任务。完成后重开 Codex、重新加载其他发起端的 MCP 工具，再由用户发起新建→续接→明确归档的真人验收。
