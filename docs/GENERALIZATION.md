# 通用接入与独立服务

日期：2026-09-22。

## 进展摘要

- **测试目标**：在 GPT 会话中委派任务，通过 MCP 联系其他模型并返回真实回复，双方均能查看记录。此前 GPT → Gemini 的单任务测试已由用户反馈效果良好。
- **通用化已完成的部分**：独立通信服务、工作者能力发现、可配置会话身份、可替换唤醒适配器；已迁移保留原有凭据和消息。模型或应用名称不再决定核心通信流程，新宿主的唤醒仍需对应适配器。
- **验证进展**：最近全套 361 项 Python 测试通过（48.10 秒）。双向聊天批准的 342 项基线之后，新增 19 项新版 MCP 回归。Claude 桌面表单受宿主回调缺失阻塞；连接器的新版协议兼容缺口已修复，Antigravity 复测却在工具许可后立即返回 cancel，用户未看到表单，任务未投递。两端聊天批准尚未通过。此前 203 项阶段新增 24 项覆盖追问续接、等待与重读计时、已关闭通道的授权到期校验及多页追问恢复。真实 E 任务的完成结果重读、F 任务等待超时后的原通道恢复均通过，未重复唤醒；G 轮真人追问续接已通过，用户补充“测试员”后 Gemini 自动续做，全程只批准最初任务一次。
- **当前验收**：单个空闲专用 Gemini 工作者的短任务已通过一次确认验收：用户只在 Codex 确认一次，随后自动唤醒、收取任务、回传结果 `391`，批准到回复约 10.95 秒。GPT、Antigravity 界面与数据库均可核对结果。用户已明确确认本轮未在接收端另行批准。
- **运行服务**：共享连接服务已由 macOS 当前用户的 LaunchAgent 托管，终止后自动恢复和现有 MCP 重连均已实测，启动不再依赖 Codex。重新登录／重启电脑后的自动加载尚未实测。
- **当前权限配置与边界**：Antigravity 专用项目仅预授权 `connector_receive`、`connector_send` 两个通信工具。文件和命令操作仍按工作者宿主权限处理。当前沿用单任务约束；忙闲识别、并发唤醒和其他宿主接入仍未验收。Antigravity sidecar 继续由其宿主管理。

## 工作者之间的英文通信验收（2026-09-21）

Claude Desktop Code 发起的 **Claude → MCP → Antigravity Gemini → MCP → Claude** 实测通过。Codex 先委派测试，Claude 调用 `connector_request_help(target="gemini")` 建立独立通道；Gemini 的实际回复直接交给 `claude_code`，随后 Claude 把结果原文和消息编号汇报给 Codex。任务与回复均为英文，两个应用中可见相同答案：

```text
Result: 731
Check: Multiplying 43 by 10 gives 430 and 43 by 7 gives 301, summing to 731.
TEST-CLAUDE-GEMINI-20260921-A
```

| 核对项 | 实际结果 |
| --- | --- |
| 直接通信通道 | `00000000-0000-0000-0000-000000000034`；requester=`claude_code`，responder=`gemini` |
| 关联回复 | 问题 `00000000-0000-0000-0000-000000000035`；回答 `00000000-0000-0000-0000-000000000036` 的 reply_to 指向该问题 |
| 批准 | Codex→Claude 使用聊天内 MCP 确认；Claude→Gemini 另经用户在 Codex 明确确认，核对原文后通过 `/admin` 提交 |
| 计时 | 内层批准到 Gemini 回复约 9.80 秒，到 Claude 汇报约 20.25 秒；不包含用户考虑批准的时间 |
| 唤醒与读取 | Claude 和 Gemini 各一次唤醒，均达到 replied；Claude 实际读取了 Gemini 的回答 |
| 收尾 | 两个原问题均已回答，两条通道均已启动空闲关闭计时；共 4 条消息，无新增异常 |
| 界面核对 | Claude 的 Local AI Connector setup 与 Antigravity 专用会话都显示同一英文答案和测试标记 |

本轮确认了直接收发能力，也明确了两处自动衔接边界：

1. 每条新 peer 通道仍需独立批准，外层委派的批准不能授予内层通道权限。现有管理批准记录 `approved_at`，不会生成 `host_elicitation` 审计行；本轮额外保留了用户确认原文及提交路径。
2. 当前派发候选覆盖未完成问题和请求方给工作者的补充回答，尚不覆盖工作者发给请求方的最终答案。因此本轮 Claude 使用有时限的 `connector_receive` 继续等待（其界面报告 4 次），没有验证“请求方结束回合后，最终答案自动唤醒它”。

尚未测试 Gemini 独立发起的新任务、并发和长任务，也未单独询问用户本轮是否出现工作者宿主工具批准。实现与宿主配置未修改，274 项自动测试为前一轮基线，本轮新增的是上述真实模型证据。详见 直接通信验证记录（`deployment/claude-gemini-peer-verification-20260921.json`，维护者本机记录，未随源码发布）。

## 双向聊天内批准升级（协议修复已加载，实际表单仍受阻）

用户独立在 Claude 发起的 `TEST-CLAUDE-GEMINI-INDEPENDENT-01` 已通过：通道 `00000000-0000-0000-0000-000000000037` 从 `claude_code` 向 `gemini` 提问，Gemini 回答 `1,081`，原问题已解决，通道关闭。用户确认成功；这一轮仍使用独立管理批准入口。

为把批准放回任意发起工作者的聊天，新增通用 `participant` profile：

| 模式 | 工具 |
| --- | --- |
| peer（兼容保留） | status、request_help、receive、send、finish |
| requester（现有 Codex） | status、delegate |
| participant（双向工作者） | status、delegate、receive、send、finish |

participant 发起任务时复用原有 `delegate` 的表单确认、原文绑定、有界等待和结果恢复；作为工作者时保留收发能力。每个端点的审批凭据只能决定自己发起的任务，服务端权限规则不变。`en-US` 已覆盖实际批准表单和进度等文案，而不仅是工具说明。保持同通道追问续答，未扩展到 A→B→A 新通道的循环任务调度。

本次实际通过 MCP 分工：Claude 返回接口补丁建议（通道 `00000000-0000-0000-0000-000000000038`），Gemini 返回审批与回归审查（通道 `00000000-0000-0000-0000-000000000039`）。两者只收到相关源码片段，返回补丁／意见；集成者复核后修改维护源码。Claude 的建议中来信收尾说明按现有“请求方收尾”约定调整；Gemini 把 cancel 描述成 cancelled 的文字未采用，程序仍按既有 denied 状态处理。

选型沿用 [MCP form elicitation](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation)。[Claude Code 官方文档](https://code.claude.com/docs/en/mcp#respond-to-mcp-elicitation-requests) 描述交互表单；[Antigravity 官方 MCP 文档](https://antigravity.google/docs/mcp) 描述 stdio 接入和刷新，但未给出明确的表单兼容保证。本机 Antigravity 含宿主自身的 `McpManager.handleElicitation` 和用户交互处理类型；[实际服务作者的报告](https://discuss.ai.google.dev/t/mcp-store-listing-request-weft-a-shared-scrumban-board-for-agents-remote-oauth-server/183376) 也支持尝试标准表单。本机 Claude SDK 包含 elicitation 回调接口，但缺少回调时会 decline。以上支持使用同一协议，不能替代两端实际弹窗与真人提交验收。

配置升级使用通用 `enable-chat-approval` 命令，要求服务停止并持有配置锁；为选定端点补独立审批凭据，保留普通通信凭据、任务身份及其他端点。私有暂存、失败回滚和重复执行均有测试。给本机准备的 [升级文件](../deployment/enable-peer-chat-approval.command) 只选择 `claude_code`、`gemini`，校验托管服务及无活动任务后升级、恢复并检查服务。旧 Claude 登记文件已兼容 participant，后续刷新绑定仍可核对原有身份。

**首轮部署验证：** 当时全套 **342 项测试通过（34.56 秒）**，较此前新增 68 项，覆盖双向 stdio、英文确认、配置迁移与模拟维护。用户已执行升级，两端 participant/en-US、审批凭据匹配、私有文件权限及服务可用性已核对；GPT requester 配置保持原状。Claude 恢复原测试会话后已加载 delegate，Antigravity 刷新 MCP 后显示五个 participant 工具。尚未把两端实际任务表单计为通过。

Claude → Gemini 的新测试通道 `00000000-0000-0000-0000-000000000040` 在创建后约 12 毫秒收到 `host_elicitation / claude_code / decline`，没有消息被投递。UI 显示 `denied`、`host_response_meta: null`，未观察到表单，不能据此认定用户拒绝。本机应用包中，Code 会话传入 `canUseTool`、`onUserDialog`，未传入 `onElicitation`；内置 SDK 0.3.275 在缺少该回调时直接返回 decline。[同类 Desktop 问题报告](https://github.com/anthropics/claude-code/issues/89858)与该证据吻合。官方的 [requiresUserInteraction 工具注解](https://code.claude.com/docs/en/mcp#require-approval-for-a-specific-tool)可要求逐次工具许可，但不提供 elicitation 响应；将它用于任务授权需单独验证可信宿主许可、任务原文绑定和审计来源，不能把普通工具执行视为用户同意。

Gemini → Claude 的 `native-gemini-claude-20260921-b` 测试在用户提交一次 Antigravity 工具许可后，返回 `host_confirmation_unavailable`。2026-09-22 核对实际界面与数据库：没有该任务通道、批准或消息，未向 Claude 投递。用户确认“完成了，我点了一次”；该次点击对应工具使用许可，未观察到任务确认表单。错误发生在创建通道之前的能力检查，该检查同时要求 form-compatible elicitation 能力与可发送宿主请求；当前错误无法区分哪项未满足，尚未捕获本次 initialize 能力原文。仅凭应用二进制包含 elicitation 实现不能推断当前客户端连接启用了它。此前 status 的一次只读许可由 Codex 操作，不计为真人任务批准。

宿主自身文件、命令与工具权限保持各自设置；本轮没有安装新的持久工具预授权规则。后续诊断复现了连接器的协议兼容缺口，不能把 Gemini 的首次错误认定为宿主不支持表单。具体记录见 升级与协作证据（`deployment/participant-approval-verification-20260921.json`，维护者本机记录，未随源码发布）。

### 新版 MCP 兼容修复（2026-09-22）

[MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr) 用多次请求交互替代服务端反向 JSON-RPC 请求。SDK 在该协议下将 `can_send_request` 设为 false，即使客户端声明了表单能力。旧版 delegate 将这个值作为所有协议的必要条件，因此会错误拒绝现代客户端；使用声明 form 能力的新版上下文已复现原错误。此前的聊天批准回归只覆盖 legacy 交互，这一缺口未被发现。

现在按协商协议选择交互方式：旧版保持 `elicit_form`，新版返回标准 `InputRequiredResult`，由宿主提交协议层 `input_responses` 后续做。SDK 对 `requestState` 加密认证，绑定完整工具参数，300 秒到期，MCP 进程重启后旧状态失效；处理器再核对通道和严格布尔确认，复用原有端点审批凭据。工具没有新增模型可填写的批准参数。

新增 19 项回归，participant 测试 **30 passed，20.81 秒**；全套 **361 passed，48.10 秒**。覆盖双向接受／拒绝／取消、重读、无表单能力、缺失或错类型响应、篡改与参数错配、撤销／过期、MCP 重启后旧状态拒绝及原任务恢复、追问无需再次批准。首次真实 stdio 运行被沙箱监听限制阻挡，不计为代码失败复现。独立复核未发现新的可操作缺陷。

Antigravity 已通过官方界面重新加载，用原 `native-gemini-claude-20260921-b` 任务完成复测。用户反馈“没看到表单，但是我选过yes了”，对应工具使用许可。实际 UI 与数据库一致：通道 `00000000-0000-0000-0000-000000000001` 创建后约 **39.29 毫秒**收到 `host_elicitation / gemini / cancel`；返回 `denied`、`host_response_meta: null`，`approved_at` 为空、消息数为 0。任务未投递给 Claude。

这次已越过原能力检查错误，但未通过实际表单验收。宿主没有给出取消原因，不能由 cancel 认定用户拒绝，也不能仅凭当前结果区分宿主回调、策略或表单处理问题。后续需先查清该边界再安排重测。此次仅记录实测证据，未更改已通过 361 项测试的实现或两端持久权限规则；Claude Desktop 的回调缺失也仍待解决。

只读核查 Antigravity 2.15.1 的程序包，仍能找到表单校验、elicitation 交互类型及回调注册接口；已查阅的官方 MCP 文档未找到表单启用设置。这些信息不能定位本次 cancel 的内部原因。

### 独立最小表单诊断（2026-09-22）

用户要求继续测试后，临时登记独立 `connector_form_probe` stdio 服务器。它不使用连接器凭据或服务，不创建任务，只请求一个字段的表单并记录协议、schema、动作与耗时；不记录输入值或原始 requestState。探针本地 **8 项测试通过（3.03 秒）**，覆盖旧版／新版协议、文本／布尔字段和接受／取消。

Antigravity 同一专用会话实际协商 `2026-07-28`，声明 `form` 能力，两项顺序调用得到：

| 最小表单 | 宿主返回 | 请求至返回 |
| --- | --- | --- |
| 单个必填文本字段 | cancel | 3.346 毫秒 |
| 单个必填布尔字段 | cancel | 1.451 毫秒 |

两项均没有输入值、没有宿主元数据。Codex 未观察到实际表单；观察到的是普通工具使用许可。该取消速度也不符合正常人工填表时间。故失败不仅出现在原布尔字段或任务内容上，在独立于连接器任务授权逻辑的最小表单中仍可复现。具体是哪一处宿主 UI／回调／策略或协议处理导致取消仍待定位，不能据此断言所有模式都不支持表单。

临时 MCP 配置项已移除，原配置语义摘要一致，宿主已刷新。生产源码仍与 361 项测试基线一致；本轮没有创建通信任务。探针和必要回归保留用于复现，诊断证据见 最小表单结果（`deployment/form-probe-20260922.json`，维护者本机记录，未随源码发布）。


### 自动取消根因定位（2026-09-27）

本机 Antigravity 已更新至 **2.17.0**。独立于 MCP SDK 的标准库 JSON-RPC 探针，在新版 `2026-07-28` 和旧版 `2025-11-25` 下均成功连接且获得 form 能力声明；文本与布尔字段四项基线分别在 0.768、0.640、1.247、0.612 毫秒返回 cancel。因此切回旧协议不能解决本次问题。

只读分析当前应用包的 `McpManager.handleElicitation` 发现：它从表单请求元数据读取 **`progressToken`**，将其格式化后查找当前交互回调；缺少请求参数、缺少该标记或回调不存在时直接返回 `cancel`。连接器原表单请求没有把工具调用的该标记传过去。

诊断服务器只增加原始 `progressToken` 的传递，其他表单结构保持不变：同一会话立即显示真正的 `value` 文本框与 `Decline`／`Submit` 按钮。2026-09-28 复核：新版文本表单显示已验证，但调用在等待三分钟后超时，未记录提交结果；旧版布尔表单在 13,512.481 毫秒后收到实际 `accept`。因此旧版表单交互往返通过，新版仅确认显示，完整提交仍待验证。生产委派代码尚未修改。这个标记只负责将表单路由回正确聊天，不能作为批准凭据或替代原任务绑定。

探针回归 **24 passed，3.56 秒**。静态函数位置、程序摘要及本轮分阶段记录见 根因诊断证据（`deployment/wire-probe-20260927.json`，维护者本机记录，未随源码发布）。两个临时服务器已移除并刷新，界面确认仅保留原有 `local_ai_connector`（5 个工具）。

### 正式表单关联修复（2026-09-28）

正式委派现在从请求上下文提取 `progress_token`，只将该值传到表单请求。旧协议使用 SDK 的底层 `send_request` 保留 `related_request_id`；新版还通过公开中间件恢复被 MCP 2.2.0 响应序列化删掉的嵌套 `_meta.progressToken`。这是协议关联修复，不按模型或应用名称分支。[MCP 规范](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/index.mdx)将该标记定义为进度关联元数据；依赖它路由表单是本次观察到的宿主行为，并非所有 MCP 宿主都必须采用的规则。SDK 外层的签名状态、原任务参数绑定及端点审批权限继续生效。

先增加真实 stdio 回归：原实现的四个针对性场景失败；修复后全套 **385 passed，62.43 秒**。覆盖双向 participant、新旧协议、缺省／数字 0／字符串标记、接受／拒绝／取消，并检查无关元数据不被转发；原状态篡改与重放测试继续通过。正式 Gemini → Claude 真人验收已显示完整任务表单，通道 `00000000-0000-0000-0000-000000000041` 随后遭遇 Antigravity 的 180 秒工具截止时间，返回 `context deadline exceeded`。核对数据库仍为 pending、批准记录为 0、消息数为 0，尚未投递给 Claude；用户是否已点击任务表单 Submit 待确认，不能将超时直接归因于用户未操作，也不能将显示计为完整往返通过。本轮证据（`deployment/form-routing-verification-20260928.json`，维护者本机记录，未随源码发布）。

## 结构

2026-09-28 后续恢复结果：用户明确批准指定 fork 后，仅更新 Claude 绑定；用户重启后台服务加载。原已批准通道收到 Claude 实际回复 **899**，双方会话均显示相同答案，dispatch `00000000-0000-0000-0000-000000000042` 为 replied、attempts=1。此次接通依赖手动恢复失效绑定，自动项目会话打开／恢复仍待实现；接收端是否另有用户批准待用户确认。

```text
MCP 宿主 → stdio 或 Streamable HTTP → 独立连接器服务 → SQLite
                                              ↓ 可选
                                     command / socket 适配器
                                              ↓
                                      宿主提供的唤醒接口
```

发起侧 stdio 的 requester 配置暴露 `connector_status`、`connector_delegate` 两个工具；peer 及既有 HTTP 入口保留五个底层通信工具。核心按端点凭据、通道和消息编号处理授权及回复，不根据模型品牌分支。

`connector_status` 新增 `self` 和 `workers`，原有 `peers` 保留兼容。每个工作者有 `id`、`name`、`description`、`capabilities`、`availability`。描述与能力属于管理员声明，不证明模型实时能力；目前 `availability` 为 `unknown`，不把登记状态当作在线状态。

`peer-add` 负责校验名称、能力说明及可选身份映射，保存私有凭据。登记须在服务停止时执行，之后重启生效。默认 `generic` 按凭据隔离；`metadata` 按管理员给定的 `namespace` 和嵌套字段路径绑定会话。原有两个宿主的名称保留为旧身份配置的兼容别名。

## 可替换唤醒适配器

`wakeup.json` 中每个端点对应一个明确目标和适配器。新宿主实现以下操作即可复用派发器：

| 操作 | 请求字段 | 成功响应 |
| --- | --- | --- |
| confirm | target | `{"ok":true,"target":{…}}`，须由宿主确认 |
| status | target | `{"ok":true,"state":"idle或busy或unknown"}` |
| send | target, dispatch_id, text | `{"ok":true,"accepted":true,"host_ref":"…"}` |
| reconcile | target, dispatch_id | `{"ok":true,"result":"delivered或not_delivered或unknown"}` |

失败响应为 `ok:false`，附 `error` 和 `detail`；允许的错误类别为 rejected、unavailable、stale_target、ambiguous。只有确定没有发送且可以重试的错误才能标记 `retryable:true`。派发器在每次副作用之前持久化意图和次数；未知结果需要核对，不能盲目重发。

- `command`：配置 `command` 参数数组，由独立服务启动命令桥接；stdin/stdout 交换一次 JSON。
- `socket`：配置绝对路径 `socket`，通过仅当前用户可访问的 Unix socket 调用桥接进程。适用于接口环境由宿主注入的应用。

桥接进程通用启动方式：

```sh
python -m local_ai_connector.adapter_socket --socket /private/path/host.sock -- python /path/host_bridge.py
```

父目录须属于当前用户且权限为 0700，socket 权限为 0600。桥接不接收 shell 命令，只执行启动时配置的程序。此边界不提供同一操作系统账户内的强隔离。Unix socket 桥接面向 macOS/Linux；Windows 仍受现有文件锁实现限制。

Antigravity 的具体实现位于 `integrations/antigravity/bridge.py`，通过官方 Sidecar 内的 `agentapi` 工作。其元数据结构、命令名和字段仅留在该适配层，其他宿主无需使用它。当前接口不能确认忙闲或核对历史发送结果，适配器如实返回 unknown。本机仍沿用用户批准的单任务测试配置，未验证并发唤醒。

## 本机部署

- 维护源码及 Python 环境：`/path/to/local-ai-connector` 与其中的 `.venv`。
- 私有数据：`~/.local/share/local-ai-connector`，已保留此前凭据与消息记录。
- 通信地址：`127.0.0.1:38475`。
- 双方 MCP 设置已指向维护源码与新数据目录。
- Antigravity 的 `codex-connector-wake-test` Sidecar 当前只运行 socket 桥接，服务不再由其启动。
- 当前独立服务由 `gui/<uid>/dev.local-ai-connector.service` 管理，配置位于用户的 `~/Library/LaunchAgents`，启用 `RunAtLoad` 和 `KeepAlive`。用户在 macOS Terminal 完成注册后，实际终止一次服务，launchd 自动恢复，现有 requester MCP 连接成功。
- G 轮真人测试前曾因服务未响应而手动恢复；当时停止原因未确认。本轮已完成从 Codex 手动进程到 LaunchAgent 的接管。当前 Codex 任务已能使用更新后的 requester 两工具，真实续接成功。
- 先前替换的应用配置条目保存在私有数据目录的 `installation-previous.json`，原测试数据目录也保留。

通用服务的启动命令：

```sh
PYTHONPATH=src .venv/bin/python -m local_ai_connector.cli --data ~/.local/share/local-ai-connector serve
```

此命令在维护源码目录执行。不要与已有服务同时启动；目录锁会拒绝第二个进程。

### 后台服务接管（2026-09-21，已验证）

已校验当前用户的 LaunchAgent 配置与维护目录版本一致，格式、解释器和源码路径有效。当前执行沙箱拒绝终止原服务，`bootstrap` 返回错误 5，系统日志查询也明确拒绝在沙箱内运行。因此用户在 macOS Terminal 执行已复核的 [后台接管与恢复验证文件](../deployment/enable-background-service.command)，完成注册和自动恢复检查。错误 5 的具体系统根因未独立确定；未为此修改系统隐私或安全设置。

| 验证项 | 实际证据 |
| --- | --- |
| 系统接管 | LaunchAgent 显示 running；原 Codex 启动的进程 89118 已结束 |
| 自动恢复 | 首个后台进程 90627 收到 SIGTERM 后结束；launchd 自动启动 90718，运行次数为 2，上次终止信号为 15；未用 kickstart 强制重启 |
| 单实例 | 90718 为端口 38475 的唯一监听进程，和 launchd 记录相符 |
| MCP 重连 | 当前聊天既有 connector_status 直接成功，无需重新加载 MCP；通道、目标目录及状态与切换前一致 |
| 数据连续性 | 10 条通道、16 条消息、7 条批准记录保留；两个 SQLite 数据库 quick_check 均为 ok，MCP incidents 为空 |
| 工作者桥接 | 配置目标的只读 confirm 成功，status 仍为 unknown；没有发送新模型任务 |

PID 是本轮核对快照，之后自动恢复会变化。此次仅改变共享 broker 的运行归属；Antigravity sidecar 仍由宿主管理。LaunchAgent 按用户登录会话工作，登录后自动加载还需注销／登录或重启实测；本次没有关闭 Codex 或工作者应用来做退出验收。[Apple 的 LaunchAgent 生命周期说明](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)

完整快照及边界见 后台服务验证记录（`deployment/service-autostart-verification-20260921.json`，维护者本机记录，未随源码发布）。本轮未修改生产实现，203 项程序测试沿用此前结果；此次新增验证为真实系统接管、自动恢复、MCP 重连和数据连续性。

## 通用层验证结果（此前）

- 全套 Python 测试：106 passed，18.86 秒。
- 新增 18 项：任意端点能力登记、私有凭据保护、注册失败回滚、重复／运行中注册拒绝、自定义宿主身份映射、socket 桥接与断开时的歧义处理、桥接不可用时继续通信。
- 已安装配置的两个真实 stdio MCP 客户端，在 Antigravity 桥接关闭时完成批准、接收、关联回复与结束流程。对端由测试程序扮演，不是模型生成结果。
- 恢复官方 Sidecar 后，经 socket 适配器只读确认了已配置会话；未调用 send，未启动额外模型任务。
- SQLite 迁移后完整性检查通过。相关标识与结果见 `deployment/verification.json`。

### 首轮真实模型端到端验收（2026-09-21，旧五工具流程）

新建 Codex 任务“通用 MCP 委派验收”，委派 Gemini 3.8 Flash High 写一首四行诗，标记为 `TEST-MCP-GENERIC-20260921-A`。

| 验收项 | 实际结果 |
| --- | --- |
| 自然语言自主选工具 | 首次未通过：工具已加载，但 GPT 先选择了 Computer Use；诊断后明确提示 MCP，才进入正确流程 |
| 用户审批 | 已保留；批准前任务未向 Gemini 投递 |
| Gemini 唤醒与回复 | 通过；1 次发送，依次记录 intent → attempt_started → acknowledged → tool_executed → replied；批准到回复约 22.9 秒 |
| GPT 接收与返回 | 通过，但 GPT 在审批等待阶段结束了本轮，需补发“继续接收已有通道”的通知 |
| 双方可核对记录 | 通过；Antigravity 会话、GPT 最终回复及连接器消息正文一致，GPT 仅增加 Markdown 换行空格 |
| 交流结束 | GPT 调用 connector_finish，通道随后为 closed |

结论：**真实 MCP 通信链路通过，自动调用体验未完全通过**。任务发送与回复由 MCP 完成；首次错误的界面操作已停止，检查方仅通过界面核对会话和审批状态。此次没有修改实现或配置，也没有重发任务。

GPT 任务 ID：`00000000-0000-0000-0000-000000000043`。通道：`00000000-0000-0000-0000-000000000044`。Gemini 会话：`00000000-0000-0000-0000-000000000045`。完整消息关联、阶段时间和源码摘要保存在 本次验收记录（`deployment/live-verification-20260921.json`，维护者本机记录，未随源码发布）。

本轮旧流程中的选工具和审批后续接问题，随后由下文两工具流程继续验证。新宿主的具体唤醒实现、当前宿主忙碌时的行为及重新登录后的服务自动加载仍未验证。

## 聊天内审批与委派体验：调研结论（2026-09-21）

目标交互：用户要求联系某个工作者 → Codex 聊天内展示目标和任务原文、批准一次 → 工作者执行 → Codex 收到真实结果。下述设计已实现并部署到本机 requester。在单个空闲专用 Gemini 工作者的短任务中，聊天内一次确认及自动回传已通过真实验收。

### 从五个底层工具到两个委派入口

原有一个 MCP 服务暴露五个工具，分别执行发现、发起、接收、回复、结束。它们适合底层双向通信，但把“开通道、等审批、循环接收、关闭”交给模型编排，导致首轮体验中断。`request_help` 立即返回 pending；`receive` 默认等 20 秒后返回 waiting，后续依赖模型再次调用，并非服务在原调用中完成整个任务。

现已在发起侧以“发现工作者”和“委派任务”为主要入口。委派工具内部完成审批、投递、有界等待、关联回复和结束交流；原有收发工具继续供工作者及兼容客户端使用。工具参数仍使用已登记端点和任务内容，不固定品牌或应用。

### 审批放在哪一层

- **任务审批**：采用 MCP form elicitation。服务在工具执行中向宿主请求确认；[Codex App Server](https://learn.chatgpt.com/docs/app-server#mcp-server-elicitation-requests) 明确支持 `mcpServer/elicitation/request` 的 form 请求及 accept、decline、cancel 响应。由可信宿主 UI 接收用户决定，再绑定到原始任务和目标；不能用模型填写的 `approved=true` 代替。本轮真实任务记录了一次接受。
- **宿主调用工具的权限**：[Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 支持按工具配置审批策略。本机仅对 status、delegate 两个入口配置放行，任务通信仍由表单确认。本轮 GPT 原调用继续等待并回传，无需补发“继续”。
- **接收方的通信权限**：[Antigravity MCP 文档](https://antigravity.google/docs/mcp) 支持按 `mcp(server/tool)` 设置权限。已通过官方界面，在专用 workspace 的项目范围内仅增加 `mcp(local_ai_connector/connector_receive)`、`mcp(local_ai_connector/connector_send)` 两条 allow 规则；磁盘双层 `permissionGrants` 核对一致。复测中接收端自动完成这两个工具调用，用户确认只在 Codex 批准一次。写文件、执行命令等操作仍按工作者宿主权限处理；当前 Sidecar 接口没有在已查文档中提供将所有桌面权限请求转交 Codex 的契约。

[MCP elicitation 规范](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation) 要求能力协商，并未保证任意宿主具有同一 UI 或能证明真人确认。本轮实际验证的是 Codex stdio requester；现有 HTTP MCP 的 stateless JSON 响应模式不提供同样的反向请求通道，后续需单独适配。不能由 Codex 的可行性推断 ChatGPT Work 或其他宿主已经兼容。

### 等待和社区方案

短任务在一次委派调用中等待真实结果，进度由程序报告。超时或中断保留任务标识，以原任务恢复查询，避免重复唤醒。本机 Codex 的委派工具超时已配置为 660 秒；连接器确认等待上限为 300 秒，批准后的执行等待另计 1–600 秒。此前 D 轮已验证确认超时后的原任务恢复；最终 E 轮批准到真实回复约 10.95 秒。尚未覆盖接近宿主超时上限的等待。[MCP Tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks) 提供持久任务、状态与延后取结果的方案，但必须核查客户端实际支持；规范中的任务通知不等于自动启动空闲模型回合。

| 参考 | 可借鉴部分 | 与当前目标的差异 |
| --- | --- | --- |
| [PAL / clink](https://github.com/BeehiveInnovations/pal-mcp-server) | 一个委派工具封装子 CLI 执行及结果返回 | 主要使用 CLI 运行时，不能据此保证现有桌面会话及双端记录；默认权限参数不适合直接照搬 |
| [llm-cli-gateway](https://github.com/verivus-oss/llm-cli-gateway) | 会话管理、异步任务、结果恢复和审批边界 | CLI/ACP 路线；其 MCP managed 审批不是所有适配器都支持，不能直接解决 Antigravity 桌面审批转交 |

源码核查：PAL 的 [clink 工具](https://github.com/BeehiveInnovations/pal-mcp-server/blob/main/tools/clink.py) 等待 `agent.run`，其[运行器](https://github.com/BeehiveInnovations/pal-mcp-server/blob/main/clink/agents/base.py) 启动并等待子进程；默认 CLI 权限包含跳过审批的参数，不能当作审批回传方案。Gateway 的 [ApprovalManager](https://github.com/verivus-oss/llm-cli-gateway/blob/main/src/approval-manager.ts) 根据策略自动决策，其文档将 `mcp_managed` 限于 Claude；Gemini/Antigravity 适配不能据此获得人类审批桥接。

维护与许可快照：PAL 为 Apache-2.0，最新发布页显示 [v9.8.2（2025-12-15）](https://github.com/BeehiveInnovations/pal-mcp-server/releases/tag/v9.8.2)，未独立查全 main 的近期提交；Gateway 为 MIT，最新稳定发布为 [v3.2.1（2026-09-09）](https://github.com/verivus-oss/llm-cli-gateway/releases/tag/v3.2.1)。本次核查聚焦委派和审批实现，未做整个项目的缺陷审计。

取舍：复用现有消息、审批审计和防重复派发逻辑，增加通用的委派入口与宿主确认处理；暂不引入整套 CLI 网关，也不把既有桌面会话替换为另一个执行进程。

### 尚待验证

一次确认后自动完成及一轮真人追问续接已在单个专用 Gemini 工作者中验证，不能据此推断忙碌或并发场景。拒绝／取消不投递已有自动测试覆盖；真实宿主中的拒绝／取消、接近宿主超时上限的等待、忙碌目标、并发任务、其他宿主接入及重新登录后的服务自动加载仍需各自验证。任务通信批准不改变工作者文件和命令权限。

## 聊天内委派实现与本轮验收（2026-09-21）

`connector_delegate` 在一次调用中打开通道、展示完整目标和原始任务、获取宿主 form elicitation 决定、投递、等待关联答案并结束通道。工具没有模型可填写的批准参数。可信 stdio 前端持有与普通端点凭据分开的审批凭据，仅能决定自身发起的任务；服务记录 `host_elicitation` 审批审计。

等待超时或中断后，复用原 target、message、request_key 恢复任务。工作者追问返回 `input_required`；同一工具的 reply_to、reply 参数提供幂等续答。已完成答案可重读，不再唤醒。

本机已更新私有 server/gpt 配置和 Codex 两工具入口，重启独立服务后已确认工具发现及原有三条 closed 记录保留。旧配置备份在私有数据目录 `deployment-before-chat-delegation-20260921`。必要文件／命令权限仍由工作者宿主执行，未将任务批准等同于全部操作放行。

**最终 E 轮结果：单个空闲专用工作者的短任务，一次 Codex 确认后自动完成。** 在任务“Codex 聊天内委派验收”（`00000000-0000-0000-0000-000000000046`）继续发起算术任务。请求编号为 `gemini-single-confirm-20260921-e`，通道为 `00000000-0000-0000-0000-000000000047`。测试前明确请用户不要在 Antigravity 批准；结果回传后，用户再次确认：“是，只在 Codex 确认了一次”。

| 核对项 | 结果 |
| --- | --- |
| Codex 聊天内确认 | 批准时间 `1790012488.3780391`；用户确认只在 Codex 批准一次 |
| 接收端权限 | 仅在专用项目预授权 receive、send 两个通信工具；本轮 Antigravity 未要求用户另行批准 |
| 自动执行与回传 | 工作者自动唤醒、收取任务并发送结果，Codex 原委派流程回传 `391`，无需补发“继续”；派发为 `replied`、`attempts=1`，阶段记录完整 |
| 实际用时 | 同一个工具调用持续 14,816 毫秒，含用户确认及执行；回复时间 `1790012499.32389`，距批准约 10.95 秒 |
| 可核对记录 | Codex 回传、Antigravity 界面及数据库均记录结果 `391`；Antigravity 界面直接显示 MCP 收取、发送两个调用 |
| 交流结束 | 已返回答案并调用结束交流，进入 120 秒空闲关闭期；核对时通道仍为 `active`，未记作已关闭 |

本次权限变更通过 Antigravity 官方界面完成，scope 为专用 workspace 项目；规则仅针对 `local_ai_connector` 的 `connector_receive`、`connector_send`。此次复测验证了这两条通信规则生效，未扩大文件、命令权限，也未改变核心按端点处理通信的通用逻辑。

本轮审计见 聊天委派验收记录（`deployment/chat-delegation-verification-20260921.json`，维护者本机记录，未随源码发布） 的 `single_confirmation_acceptance`。

D 轮历史缺口：通道 `00000000-0000-0000-0000-000000000048` 在确认超时后以原 key 恢复，真实诗歌回传和双方记录一致，但用户仍需在 Antigravity 批准通信工具，因此当时未达到一次确认目标。上述项目级两条规则用于修复这个权限配置缺口，E 轮才完成最终体验验收。

此前故障：前两次桌面尝试在数毫秒内收到宿主 `cancel`，均未投递。根因是 Codex 的 `McpElicitationSchema` 拒绝 SDK 自动添加的根级 `title`，不能解释为用户拒绝。已使用本机二进制生成的 schema 复现，并通过删除根级 title 修复；字段 title 和严格布尔确认保留。历史证据见 聊天委派验收记录（`deployment/chat-delegation-verification-20260921.json`，维护者本机记录，未随源码发布）。

E 轮时的全套 179 项通过；当时新增 73 项覆盖审批凭据隔离、跨端点拒绝、撤销／过期、审计事务、接受／拒绝／取消、无 elicitation 能力、审批并发状态变化、执行总时限、结果恢复及追问续答。自动测试中的 stdio 工作者及确认回调由程序扮演，与上述真实模型验收分开记录。

## 健壮性续接验证（2026-09-21）

本轮修复了追问后的执行缺口：工作者结束回合并空闲后，请求方的补充回复现在可触发独立唤醒；补充被读取记为 `delivered`，原任务收到最终回答才记为 `replied`。等待用户不消耗工作者回复时限，首次消费补充持久化刷新计时，重读同一补充不延长时限。另补齐已关闭通道在授权到期后的正文读取校验，以及多页追问的恢复读取。

全套结果为 **203 passed，26.56 秒**。新增 24 项中，17 项覆盖续接和唤醒，7 项覆盖其他恢复与边界；修复前两组复现分别出现 8 项和 5 项失败，共 13 个失败用例。其余新增用例用于补充边界与对照验证。

| 验证 | 实际结果 |
| --- | --- |
| 程序续接回归 | 多轮追问各唤醒一次；读取、重试、迟到确认及重启保持去重；等待用户、首次消费和重复读取的计时正确；忙闲与授权边界有覆盖 |
| 真实 E 完成结果重读 | 9 毫秒返回已有结果；仍为 1 次批准、1 条问题、1 条答案、1 次唤醒 |
| 真实 F 等待恢复 | 首次执行等待 10 秒后返回 `running`，复用原任务后 11 毫秒返回同一通道的 `completed`，总唤醒仍为 1 次 |

F 原计划验证拒绝，但实际审计为 `accept` 且任务已投递；用户在界面中的实际选择仍待确认。因此 F 只计入等待恢复验证，真人拒绝路径仍未通过。上述健壮性验证完成时，新增追问唤醒仅有程序回归；随后完成的真人复测记录如下。

本轮记录见 聊天委派验收记录（`deployment/chat-delegation-verification-20260921.json`，维护者本机记录，未随源码发布） 的 `robustness_followup`。

## G 轮真人追问续接（2026-09-21）

在当前 Codex 任务中直接使用 requester MCP，委派 Gemini 写一句包含称呼的问候语，起初不提供称呼。Gemini 通过 MCP 追问“请问我该怎么称呼你？”，并结束该回合。用户在 Codex 回答“测试员”后，以原任务参数和追问 ID 续答，Gemini 自动获得第二次唤醒并完成原任务。

真实返回原文：

```text
测试员您好，很高兴与您交流！
TEST-CODEX-DELEGATE-20260921-G
```

| 核对项 | 实际结果 |
| --- | --- |
| 当前聊天直接委派 | 使用 `connector_delegate`，首次返回 `input_required`，补充后返回同一通道的 `completed` |
| 一次批准 | 数据库仅 1 条 `host_elicitation/accept`；用户确认“是，只批准最初任务一次” |
| 消息关联 | 共 4 条：原任务、Gemini 追问、用户补充、最终答案；补充关联追问，最终答案关联原任务，两个问题均已解决 |
| 唤醒与去重 | 共 2 次必要派发，各 `attempts=1`；原任务 `replied`，补充 `delivered`，该通道未出现多余消息或发送尝试 |
| 回合与双方记录 | Antigravity 界面显示追问后结束回合、收到补充后通过 `connector_send` 回复；正文与 Codex 工具返回及数据库一致 |
| 续接用时 | 补充委派调用约 21.73 秒返回；这是本次观测，不是延迟保证 |

请求编号为 `gemini-clarification-20260921-g`，通道为 `00000000-0000-0000-0000-000000000049`。通信全程使用 MCP；界面只用于只读核对。此次未修改实现，源码摘要与 203 项测试对应版本一致，未重复运行全套测试。独立只读复核时通道仍为 active，不能把最终答案到达等同于即时关闭。

该结果证明当前专用工作者的一轮真人追问可以自动续接。记录见 聊天委派验收记录（`deployment/chat-delegation-verification-20260921.json`，维护者本机记录，未随源码发布） 的 `clarification_live_acceptance`。

## 选型依据

沿用已有 [MCP 工具发现与说明机制](https://modelcontextprotocol.io/specification/draft/server/tools)，保留已验证的审批和消息状态机。参考 [A2A 的身份及能力卡片思路](https://github.com/a2aproject/A2A/blob/main/docs/topics/agent-discovery.md) 增加轻量端点资料，没有引入 A2A 运行时，也不声称实现 A2A 协议。进程分离使用 [Python 标准库 Unix streams](https://docs.python.org/3/library/asyncio-stream.html)，不增加消息队列或第三方依赖。

## Claude Desktop Code 接入（2026-09-21）

用户指定目标为 Claude 桌面应用的 Code 标签页，连接指令使用美式英文。专用项目位于 `/path/to/claude-code-workspace`。本机 app 为 2.2553.1，bundled engine 为 2.1.275，界面模型标签为 Fable 5.1。

### 接入路线与证据

官方 [Desktop 配置说明](https://code.claude.com/docs/en/desktop#shared-configuration) 支持项目 `.mcp.json` 和项目指令。[Channels](https://code.claude.com/docs/en/channels) 要求会话级启用参数，尚未核实当前 Desktop 提供该入口。此次采用官方另行说明的 [session inbox](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket)：由专用会话自己的 SessionStart hook 提供地址和会话 ID，再向该显式目标发送本机消息。

单行 JSON 帧参考了 MIT 项目 [claude-code-socket-transport/message.go](https://github.com/PeterSR/claude-code-socket-transport/blob/main/message.go)，使用 Python 标准库实现最小发送，没有引入完整客户端依赖。该参考项目仅声称验证过引擎 2.1.233；本机 2.1.275 的实际投递已通过。官方未完整发布帧和回执的稳定 SDK 契约，升级后需要复核。

真实 probe：目标空闲时，程序向它的 UDS 写入一次英文测试消息；Desktop Code 显示来源为另一会话的消息，自动开始回合并回复 `CLAUDE-INBOX-PROBE-READY`。发送未使用 token 或特权身份字段。证据在 inbox-probe.json（`inbox-probe.json`，维护者本机记录，未随源码发布）。随后通过下述真实 MCP 领取任务和关联回传验收。

### 实现与验证

- `mcp_locale` 是端点私有选项，支持 `en-US`、`zh-CN`，默认中文。它选择 stdio MCP 名称、指令和工具描述，不改变工具名称、schema、消息正文或共享 HTTP 接口；不是按模型／app 分支。核心业务错误详情仍是原有中文。
- `integrations/claude_code/bridge.py` 沿用 CommandAdapter，匹配 hook 记录中的会话、工作区、socket 路径及设备／inode，并检查文件与 socket 的 owner-only 权限。新会话不会自动替换绑定。
- socket 写入没有同步应用回执，因此 bridge 返回 ambiguous；派发器保留 `sent_unconfirmed`，以实际 receive／send 推进证据。状态及 reconciliation 为 unknown，不以沉默判断未投递，也不盲目重发。
- 新增 21 项语言／登记测试和 50 项假 socket 桥接测试；全套 **274 passed，30.15 秒**。覆盖多实例语言隔离、真实 stdio 初始化、契约不变、socket 身份替换、权限、写前变化、部分写入及无回执。

### 当前部署状态

用户已在 macOS Terminal 完成独立 `claude_code` 端点登记，根流程核对到 launchd PID 92276、三个工作者可发现、SQLite quick_check 正常且 Gemini 绑定保留。真实 stdio 初始化返回英文指令和五项英文工具说明。Claude 在重开后加载项目 MCP，实际调用 `connector_status` 并报告身份 `claude_code`。

实际发现一个恢复边界：新 MCP 配置没有在既有 Desktop 回合中热加载，重开应用并恢复原会话后才加载；同一引擎会话 ID 的 socket 地址随进程变化。桥接现只固定会话 ID 与工作区，逐次核验它自己 hook 记录中的当前 socket；连接前后地址快照不同则不写入。8 项新增恢复边界测试已包含在 274 项中。用户已执行 `--refresh-binding` 更新，服务恢复为 PID 92889，根流程重新确认目标成功。恢复原会话需要其引擎已初始化并执行 hook；仅打开 Claude 首页不等于工作者已可用。

本轮保持 Claude 现有 Auto 权限模式，专用项目的持久 receive／send 预授权规则未安装。首轮保持专用空闲会话、单任务、最多一次发送；并发和忙碌目标未验证。

### 真实英文任务往返

请求编号 `claude-desktop-roundtrip-20260921-a`，通道 `00000000-0000-0000-0000-000000000050`。Codex 调用 `connector_delegate`，用户在聊天中确认后，Claude 空闲会话被唤醒，通过 `connector_receive` 领取英文算术任务，再通过 `connector_send` 回传原始问题的关联答案。Codex 同一次委派返回 `completed`。

```text
Result: 703
Check: 37 × 19 = 37 × 20 − 37 = 740 − 37 = 703.
TEST-CODEX-CLAUDE-20260921-A
```

审计：1 条 `host_elicitation/accept`，1 条问题、1 条关联答案，1 次派发且 `attempts=1`。阶段依次为 `intent → attempt_started → sent_unconfirmed → tool_executed → replied`；没有把单向 socket 写入伪记为宿主确认。批准到答案约 13.56 秒，这是本次测量而非延迟保证。Claude 界面明确显示 receive、send 工具记录及同一答案，后续审计时通道已 `closed`，本轮 Claude 端点没有新增 incident。

模型任务通信全部经连接器；UI 用于接入配置和只读核对。用户明确确认“是，只在 Codex 确认一次”，未在 Claude 或 Connector 另点批准。因此当前 Auto 模式下的空闲单任务“一次确认后自动完成”已验收。完整证据见 Claude Code 验证记录（`deployment/claude-code-verification-20260921.json`，维护者本机记录，未随源码发布）。

### 真人提交结果（2026-09-28）

原通道超时后以相同任务恢复，用户直接 Submit、未勾选 Confirmed，服务正确记录 decline 且不投递。新通道 `00000000-0000-0000-0000-000000000003` 中，用户确认勾选并提交，实际记录 `host_elicitation / gemini / accept`，原问题已写入通道。正式新版表单提交到服务的批准路径通过。完整往返尚未通过：Claude 旧会话的记录仍在，但对应收件 socket 已消失，适配器报告 `wake_stale_target` 并停止发送。当前界面可见的是不同 UUID 的 “Local AI Connector setup (fork)”；重新绑定需用户明确选择，不能自动改投其他会话。
