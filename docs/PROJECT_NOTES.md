# 本地 AI 连接器：项目笔记与代码说明

更新日期：2026-09-28  

**本轮最新结果：直接批准、无 Confirmed 复选框。** 共享确认表单已调整，Codex → Claude 返回 **35**，用户确认直接批准即可；Antigravity → Claude 返回 **221**，表单只有 Decline / Submit，双方会话记录已核对。Claude Desktop Code 发起测试仍在约 **12.425 毫秒**返回 decline，未观察到表单、未投递消息，因此三端体验尚未全部通过。全套自动测试 **384 passed，65.63 秒**；数量变化源于移除复选框后替换了旧的“accept 但未勾选”测试。详细实现报告按用户要求后续整理。本轮证据（`deployment/single-action-approval-20260928.json`，维护者本机记录，未随源码发布）。

上一轮 Gemini → Claude 的 **899** 任务已完成，包含指定 fork 的手动改绑；自动项目会话打开／恢复仍待实现。上一轮证据（`deployment/form-routing-verification-20260928.json`，维护者本机记录，未随源码发布）。

**Claude 发起侧复核：** 本机 Claude 2.9939.2 的应用包仍含缺少 `onElicitation` 时直接返回 `decline` 的 SDK 分支；检索到的回调引用均位于 SDK，未找到应用注册。结合上一轮 12.425 毫秒拒绝，当前不重复原样测试。需修复宿主接入，或验证官方 `anthropic/requiresUserInteraction` 强制逐次工具确认能否在 Desktop 中承载完整任务授权；后者尚未实测，不作为已完成的替代方案。

代码版本：0.1.0  
依据：当前源码、384 项自动测试结果及真实模型验收记录。本文说明当前实现与维护入口；详细调研和验收证据见 [通用化说明](GENERALIZATION.md)，早期桌面接入记录见 [验证记录](VALIDATION.md)。

## 一、项目目前做到了什么

项目为支持 MCP 的模型和工作者提供独立的本机通信服务。请求方发现目标后委派任务，用户在聊天中确认，连接器负责投递、按配置唤醒工作者、等待和返回真实回复。双方可在同一授权通道内追问和续答，并保留消息记录。核心按端点、通道和消息编号工作；宿主专有身份与唤醒接口留在配置或适配层。本机共享服务已交由 macOS LaunchAgent 运行，终止后自动恢复及 MCP 重连已通过；重新登录后的自动加载尚待实测。

**当前验收：** Codex → Antigravity（Gemini 3.8 Flash High）的单个空闲工作者短任务已通过：只在 Codex 确认一次，批准后约 10.95 秒回传 `391`，双方记录一致。已完成结果重读、执行等待超时后的原任务恢复也已实测，均未重复唤醒。G 轮真人追问续接已通过：Gemini 追问并结束回合，用户在 Codex 补充“测试员”后自动续做，返回“测试员您好，很高兴与您交流！”。只批准最初任务一次，共 4 条关联消息、2 次必要唤醒，每次尝试 1 次。G 轮时的自动测试基线为 **203 passed，26.56 秒**，该轮真人验收未修改源码。

**Claude Desktop Code 完整往返已通过：** Codex 批准任务后，专用空闲会话自动领取任务，通过 MCP 回传 `703`、英文计算过程和关联标记，桌面显示相同结果。审计为 1 次批准、2 条消息、1 次唤醒尝试，约 13.56 秒回传。已实现按端点选择 `en-US` MCP 指令及工具说明，宿主桥接固定会话 ID 并逐次核验它报告的当前收件地址。新增 71 项回归后，全套为 **274 passed，30.15 秒**。用户确认仅在 Codex 批准一次，Claude 和 Connector 没有额外批准。本轮沿用 Claude 的 Auto 模式，未添加持久工具预授权。

**Claude → Antigravity 直接通信已通过：** Claude 自行通过 MCP 创建发给 `gemini` 的英文请求，Gemini 直接向 `claude_code` 回复 `731` 和英文计算说明，Claude 再向 Codex 汇报。双方界面与消息关联记录一致。内层批准到 Gemini 回复约 **9.80 秒**，到 Claude 汇报约 **20.25 秒**；两条通道共 4 条消息，两端各唤醒 1 次，没有新增异常。本轮分别批准 Codex→Claude 和 Claude→Gemini 两条通道；后者由用户在 Codex 确认后，通过现有管理接口提交。Claude 保持有时限的 receive 等待，当前最终答案尚不会自动唤醒已结束回合的请求方。未修改实现，自动测试沿用此前 274 项基线。完整证据（`deployment/claude-gemini-peer-verification-20260921.json`，维护者本机记录，未随源码发布）。

**早期排查记录（当前结果以上方最新验收为准）：** 新增通用 participant 模式，同一端点可以发起需聊天确认的委派，也能接收并回复任务，确认文案支持英文。Claude 和 Gemini 经 MCP 参与补丁建议与审查，用户执行升级后，两端均已加载 delegate。随后修复连接器对新版 MCP 表单协议的兼容缺口，全套 **361 passed，48.10 秒**。实际 Claude Desktop Code 调用因缺少 elicitation 回调立即 decline。Antigravity 重新加载后已越过原能力检查错误，但用户点击工具许可 Yes 后没有看到表单；通道 `00000000-0000-0000-0000-000000000001` 创建约 39 毫秒后收到宿主 cancel，元数据为空，任务未投递。不能把这个 cancel 记为用户拒绝；独立最小表单复测也得到相同结果：文本字段约 3.35 毫秒返回 cancel，布尔字段约 1.45 毫秒返回 cancel，宿主明确声明了表单能力。这将问题缩小到宿主表单交互／协议处理边界，2026-09-27 已定位 Antigravity 的具体原因：表单请求缺少用于查找会话回调的 `progressToken`；独立探针补传后已显示真正的文本表单；2026-09-28 复核，旧版布尔表单已返回实际 accept，新版文本调用则等待三分钟后超时，未完成提交验证。临时探针已移除。2026-09-28 修复已接入正式委派，额外处理 MCP 2.2.0 对新版嵌套元数据的丢弃；全套 385 项通过。Gemini → Claude 正式表单已显示，但调用在宿主 180 秒截止时间后超时；批准记录与消息数均为 0，用户是否已提交表单待确认；Claude 发起侧的表单回调问题仍单独存在。此前用户独立发起的 `47 × 23` 测试已通过独立管理批准回传 `1,081`。升级与协作证据（`deployment/participant-approval-verification-20260921.json`，维护者本机记录，未随源码发布）。

项目仍为开发预览。受控文件写入和模型异常解释是独立配套能力；真实文件或代码协作、忙碌目标、并发及其他宿主接入需要分别验收。

| 能力 | 当前实现程度 |
| --- | --- |
| 通用端点与能力发现 | `generic` 凭据身份、工作者目录及配置式元数据绑定已实现 |
| 聊天内委派 | requester 暴露 status、delegate 两个工具；一次确认后自动等待真实回复已实测 |
| 接收方通信 | peer 和 HTTP 保留五个底层工具；Antigravity 专用项目预授权 receive、send |
| 空闲任务唤醒 | 可选 command／socket 适配器已集成；当前真实验收对象为专用 Antigravity 会话 |
| Claude Desktop Code | 英文任务从 Codex 委派、空闲唤醒、MCP 领取与回传已通过；双方记录一致 |
| 工作者直接通信 | Claude 发起、Antigravity 回复的英文 MCP 往返已通过；新通道单独批准，请求方保持 receive 等待 |
| 双向聊天内批准 | 361 项生产测试通过；Claude 缺少表单回调；Antigravity 的独立文本和布尔表单均立即 cancel，两端仍未通过 |
| 追问与补充续接 | 程序回归及 G 轮真人续接通过；用户补充后自动唤醒，沿用原任务批准 |
| 超时与结果恢复 | 原任务恢复与完成结果重读已实测；关闭后授权到期仍限制结果读取 |
| ZCode 桌面接入 | 早期工具加载、原生绑定、长等待和消息往返已实测；其空闲激活尚未验证 |
| 文件保护 | 实现受控写入锁、内容版本检查、原子替换和路径限制 |
| 代码并行隔离 | 测试了 Git worktree；使用 Git 原生命令，尚未做自动管理器 |
| 原生控制窗口 | 可查看求助、批准／拒绝／撤销、配置模型、测试和解释异常 |
| 监管模型 | 接口可用、本地调用已实测；建议质量不足，默认不启用 |

## 二、整体结构与调用关系

MCP 是 agent 调用工具的协议。stdio 客户端启动适配进程；HTTP 客户端直接访问标准 `/mcp`。两条路径共用消息规则；聊天内表单确认使用 stdio requester 或 participant，既有无状态 HTTP 入口保留 peer 工具。

```text
请求方 stdio → mcp_server.py → delegation.py → client.py → /call、/decision
工作者 stdio → mcp_server.py → client.py → /call
HTTP MCP     → server.py /mcp → peer 工具定义
                                    │
                                core.py Broker → state.sqlite3
                                    │
                              wakeup.py Dispatcher → wakeup.sqlite3
                                    │
                              command／socket 适配器 → 宿主唤醒接口

用户 → ui.swift 或 cli.py → server.py /admin → core.py
用户 → 模型解释按钮 → cli.py → supervisor.py → 本地／云端模型
用户或工作者 → 文件命令 → files.py → 指定工作区文件
```

通用端点按凭据隔离；工作者资料由 `registry.py` 校验，原生或配置式任务绑定由 stdio 路径中的 `identity.py` 可选处理。宿主调用分别放在 `integrations/antigravity/bridge.py` 和 `integrations/claude_code/bridge.py`。

`cli.py` 是统一启动入口。消息业务的最终判断集中在 `core.py`；界面和 MCP 工具都通过同一套服务调用它。

文件写入和模型解释由独立命令提供。任务通信的确认与工作者自身的文件、命令权限分别处理。

## 三、逐个源码模块说明

### 1. cli.py：启动和管理入口

[查看源码](../src/local_ai_connector/cli.py)

主要由 `main()` 解析命令，再转交对应模块处理。

| 命令／函数 | 具体做什么 |
| --- | --- |
| `save_private()` | 以仅当前用户可读写的权限创建配置文件；已有文件会报错，避免直接覆盖 |
| `init` | 校验端口和两个以上端点名称，生成独立凭据；可显式选择原生绑定 |
| `serve` | 先取得服务目录的独占锁，再启动只监听 `127.0.0.1` 的 HTTP 服务 |
| `mcp` | 启动提供给桌面客户端的 MCP 适配器 |
| `client-config` | 输出通用 JSON、Codex TOML 或 ZCode stdio 配置；支持导出 HTTP 地址与端点凭据 |
| `peer-add` | 服务停止时登记端点、能力说明及可选身份映射，拒绝覆盖已有身份 |
| `service-config` | 导出 macOS launchd 配置；注册和加载由部署步骤完成 |
| `wakeup-status` | 只读查看派发状态、尝试次数和事件记录 |
| `status` | 使用管理员接口读取全部通道及异常状态 |
| `approve` | 显示原始求助，输入 `yes` 批准，其余输入拒绝 |
| `revoke` | 撤销指定通道 |
| `call` | 通过同一 HTTP 接口执行诊断操作 |
| `file-version`、`file-write` | 转交文件版本读取与受控写入 |
| `model-config`、`model-test` | 保存模型配置，或调用一个固定中文样本测试连接 |
| `model-explain` | 从管理员接口查找指定异常，把错误码和说明交给模型解释 |
| `ui` | 在 macOS 上编译 Swift 窗口，生成本地应用包并启动 |

`init --peers` 默认将所有端点设为 generic；`--native-peer 名称=codex` 或 `名称=zcode` 才启用原生绑定。`client-config --client` 只选择输出格式，不改变身份模式。初始化拒绝重复名称、路径越界和保留名称，并在写入前检查已有配置。

### 2. core.py：消息规则和状态存储

[查看源码](../src/local_ai_connector/core.py)

这是项目的核心。`Broker` 持有 SQLite 数据库连接和一个异步事件通知对象，决定当前操作是否允许、消息应交给谁，以及等待何时结束。

| 方法 | 职责 |
| --- | --- |
| `register()`、`authenticate()` | 登记端点、保存凭据摘要，根据凭据识别请求方 |
| `channel()` | 查询通道，并检查调用端点是否属于该通道 |
| `open()` | 校验求助内容、目标、去重编号与期限，创建待批准通道 |
| `decide()` | 批准或拒绝；宿主决定与审计、首次问题投递在同一事务中写入 |
| `_message()` | 统一写入消息，生成消息编号，确定另一方为接收方 |
| `send()` | 校验通道、去重编号和回复对象，写入回复或追问 |
| `receive()` | 按接收方、通道和游标查找消息；没有新消息时等待事件或期限 |
| `delegation_result()` | 仅向发起方返回原始问题的关联答案；关闭后可在原授权期内重读，到期或撤销后不返回正文 |
| `_observe()` | 将实际读取消息通知派发器；观察器异常记录日志与 incident |
| `finish()` | 确认全部问题已回答，再开始空闲关闭倒计时 |
| `expire()` | 根据总期限和空闲期限更新状态 |
| `revoke()` | 将通道设为已撤销，并通知等待者 |
| `snapshot()` | 返回本端身份、工作者目录、有权限查看的通道，以及最近最多 20 条异常 |
| `record_incident()` | 保存端点、错误码、说明和发生时间 |
| `notify()`、`close()` | 唤醒等待者、关闭数据库连接 |

`ConnectorError` 是统一业务错误，带有机器可识别的错误码和中文说明；`require()` 用于条件不满足时立即抛出该错误。

**消息对应方式：** 每条问题有独立 `id`。回答必须带 `reply_to`，指向同一通道中对方尚未回答的问题。追问会生成另一个未完成问题，所以回答追问不等于完成最初求助。

**去重方式：** 创建求助按“请求端点＋求助编号”去重，消息按“发送端点＋消息编号”去重。相同编号携带不同内容会报错。发送重试仍须满足通道有效等条件，不能把去重理解成到期后继续发送的许可。

**等待方式：** 使用 `asyncio.Condition` 挂起等待，消息或状态改变时唤醒。等待期限到达后返回 `waiting`；MCP／HTTP 接入的单次等待默认 20 秒，上限 3600 秒；内部 Broker 的历史函数默认值仍为 300 秒，接入层始终显式传入等待时长。每次最多返回 100 条消息，用末条序号作为下一次读取游标。

### 3. server.py：本机接口与请求校验

[查看源码](../src/local_ai_connector/server.py)

`create_app()` 用 Starlette 建立 HTTP 服务，将外部请求转换成 `Broker` 调用。

| 入口 | 使用者 | 能做什么 |
| --- | --- | --- |
| `/mcp` | generic 端点的标准 MCP 客户端 | 工具发现与调用，使用 Streamable HTTP |
| `POST /call` | 持有端点凭据的客户端 | 发起求助、收发消息、读取委派结果、结束交流、查询本端状态 |
| `POST /decision` | 持有独立审批凭据的可信 stdio 前端 | 为自身发起的任务提交 accept／decline／cancel 并记录审计 |
| `GET /admin` | 持有管理员凭据的控制入口 | 查看全部通道和最近异常 |
| `POST /admin` | 管理员入口 | 批准、拒绝、撤销 |

`Operation` 使用 Pydantic 严格校验字段、类型和范围，拒绝额外字段。读取请求体时限制总大小为 100,000 字节。`auth()` 区分端点与管理员凭据，并拒绝带浏览器 `Origin` 的请求；服务还检查 `Host`。

`MCPAuthMiddleware` 在 MCP 处理前校验每个 HTTP 请求的凭据与来源，将认证端点放入请求上下文，并拒绝原生绑定端点使用 HTTP。`/mcp` 使用官方 SDK 的 stateless 模式和 JSON 响应，应用消息状态仍持久化。服务生命周期管理 SDK 及可选唤醒派发器，关闭时先停派发器再关闭 Broker。

`approval_tokens` 将独立审批凭据绑定到请求端点，须区别于管理员及普通端点凭据。`/decision` 只接受通道和决定，校验请求端点、通道状态与审计幂等性；模型提供的任务参数不能直接授予批准。

`/call` 接收消息时同时等待“业务结果”和“HTTP 断开”。客户端断开后取消服务端等待任务，防止一个已取消的请求长期占用服务。取消等待不会删除 SQLite 中的消息。

`error()` 把 `/call` 业务错误转换成 HTTP 错误响应；MCP 路径转换为工具错误。两条路径都为已识别端点记录业务异常。目前并非所有错误都会进异常表：身份适配器错误、文件命令错误、模型错误和未认证请求不经过这条记录路径。

### 4. client.py：本机 HTTP 客户端

[查看源码](../src/local_ai_connector/client.py)

`Client` 从端点配置读取服务地址和凭据，创建异步 HTTPX 客户端。`call()` 向 `/call` 发送 JSON，`decide()` 使用独立 `approval_token` 向 `/decision` 提交宿主决定；两者把业务错误恢复成 `ConnectorError`。`close()` 释放连接。

它关闭环境代理继承，避免本机请求因为代理环境变量而走其他路径。等待请求的 HTTP 超时比业务等待多 10 秒。发送没有自动重试；调用方必须保留原去重编号，根据错误明确决定如何恢复。

### 5. identity.py：桌面任务身份绑定

[查看源码](../src/local_ai_connector/identity.py)

`caller_session()` 从宿主附带的元数据识别当前任务：Codex 读取 `x-codex-turn-metadata.thread_id`，ZCode 读取 `com.zcode/request-context.session_id`。generic 返回无原生任务身份，调用方按凭据确定端点，不再生成随机进程 UUID。旧 generic 绑定字段保留在文件中，但不参与判断。

`metadata` 模式通过端点配置的 `identity.namespace`、`identity.path` 读取其他宿主的嵌套身份字段；CLI 对应参数为 `--session-namespace`、`--session-path`。缺失字段时拒绝绑定，端点身份模式与界面显示的模型名称分开。

原生模式的 `bind_session()` 对端点配置加进程间锁，首次调用时写入 `bound_session`；后续若换成另一个任务，则返回 `session_conflict`。配置更新采用临时文件原子替换。

任务身份由宿主元数据提供，工具参数没有一个可让模型自行填写的任务身份字段。但这仍依赖本机配置和凭据：同一个操作系统账户下、可以读取这些文件的进程之间，不具备强安全隔离。

### 6. mcp_server.py：让桌面 AI 调用连接器

[查看源码](../src/local_ai_connector/mcp_server.py)

`create_mcp()` 使用官方 MCP SDK 的 `MCPServer` 注册工具。`serve()` 根据端点的 `tool_profile` 选择 peer、requester 或 participant；requester 与 participant 须配置独立审批凭据。participant 暴露 status、delegate、receive、send、finish，兼顾任务发起与接收。`server.py` 将 peer 工具挂到 Streamable HTTP。

stdio 端点可用 `mcp_locale` 选择 `en-US` 或 `zh-CN`，缺省中文；`peer-add --mcp-locale en-US` 将其写入该端点私有配置。名称、指令和工具说明按实例选择，工具名、参数和业务行为相同。共享 HTTP 实例保持原有语言。此选项不替换宿主系统提示，核心业务错误说明仍使用原有中文。

requester 向发起方提供两个入口：

| AI 工具 | 作用 |
| --- | --- |
| `connector_status` | 查看 `self`、`workers`、通道与异常，发现可用目标 ID |
| `connector_delegate` | 展示任务确认、投递、等待结果；通过原任务参数恢复等待或补充回复 |

peer／HTTP 提供以下五个工具：

| AI 工具 | 映射的操作 | 作用 |
| --- | --- | --- |
| `connector_request_help` | `open` | 创建待批准求助，返回通道信息 |
| `connector_receive` | `receive` | 等待某通道或本端全部新消息 |
| `connector_send` | `send` | 回复问题或提出追问 |
| `connector_finish` | `finish` | 在问题全部完成后开始空闲关闭计时 |
| `connector_status` | `status` | 查看本端通道、异常和已登记端点 |

工具共用消息规则及错误转换。stdio 的 `invoke()` 对原生模式读取宿主元数据、验证任务绑定，再发送本机 `/call` 请求；HTTP 的 `invoke()` 从已认证请求取得端点，经同一参数校验和操作分发调用 Broker。业务错误转换成 SDK 的 `ToolError`，网络错误提示以原编号恢复。

`lifespan()` 在 MCP 进程退出时释放 HTTP 客户端。工具说明提示模型等待真实消息、按问题编号回复，但权限和状态限制由核心代码实际检查。

### 7. delegation.py：一次调用中的任务确认与恢复

[查看源码](../src/local_ai_connector/delegation.py)

`delegate()` 把开通道、表单确认、等待、追问和结束交流串在一次工具调用中。新任务按协议版本使用 `elicit_form()` 或 `InputRequiredResult` 请求展示目标与完整原文，接收严格布尔确认；能力缺失时明确报错。表单根级 schema 不携带 SDK 自动生成的 `title`，以满足已核查的 Codex schema。取消响应保留宿主元数据，不能仅凭 cancel 推断用户主动点击了取消。

| 返回状态 | 调用方如何继续 |
| --- | --- |
| `approval_timeout` | 确认等待结束；复用原 target、message、request_key 恢复 |
| `running` | 本次执行等待到时；任务仍可用原参数查询，避免再次委派 |
| `input_required` | 展示未解问题，用其 ID 作为 reply_to、补充内容作为 reply 续答 |
| `completed` | answer.body 为真实关联答案 |
| `denied`、`expired`、`revoked` 等 | 按实际状态结束或向用户说明 |

确认等待上限 300 秒；批准后的执行等待默认 180 秒、可设 1–600 秒，整体截止时间覆盖网络调用和分页读取。任务授权固定为创建后 1 小时。补充使用稳定消息键去重；已有答案仍有未解追问时逐页查找，避免只读前 100 条导致空的追问列表。

### 8. registry.py：工作者登记与能力目录

[查看源码](../src/local_ai_connector/registry.py)

`PeerProfile` 校验 name、description、capabilities；`add_peer()` 在服务停止时创建端点私有配置并更新 `server.json.peer_profiles`，失败时回滚本次新增文件。`connector_status` 同时返回兼容的 peers 和带描述的 workers。能力是管理员声明，当前 availability 为 unknown，不能据此判断宿主在线或空闲。

### 9. wakeup.py：可选派发器与持久恢复

[查看源码](../src/local_ai_connector/wakeup.py)

`load_config()` 校验目标、适配器和超时；存在 `wakeup.json` 且 enabled 为 true 才启用。`Wakeup` 管理后台任务与关闭，`Dispatcher` 决定何时唤醒，`WakeStore` 记录意图、消息覆盖、实际读取和事件。错误配置会报告并停用可选适配器，普通通信服务继续运行。

- 每次发送（含重试）先确认目标、检查宿主状态，再刷新授权；`begin_attempt()` 在副作用前持久化进行中状态与次数。
- 默认忙碌或 unknown 时延期；结果不明确时先 reconcile，不能盲目重发。只有确认未送达且满足条件时才在次数上限内重试。
- 实际投递按消息 ID 记录，某通道读取新消息不会遮蔽其他通道的旧消息。晚到确认不能覆盖读取或终态证据。
- 工作者追问后，原任务未完成时的新补充可单独派发；补充读取记为 delivered，原任务最终回答记为 replied。普通最终答案不会因此触发新唤醒。
- 等待用户或未读补充时不计工作者回复超时；首次读取补充刷新持久计时，同一补充重读不会延长截止时间。总授权期限和撤销始终生效。

主要阶段为 intent → attempt_started → acknowledged → tool_executed → replied；补充派发可结束于 delivered。acknowledged 只表示宿主接受，实际读取和最终回答由连接器观察证明。唤醒文本含派发编号和读取游标，消息正文经原有通道读取。

续接问题优先检查 `_candidates()` 的消息筛选、`_waiting_for_input()` 的等待判定，以及 `on_receive()` 的实际读取与计时刷新。

### 10. adapter_socket.py 与宿主桥接

[socket 适配器](../src/local_ai_connector/adapter_socket.py) · [Antigravity 桥接](../integrations/antigravity/bridge.py)

`CommandAdapter` 执行配置的命令数组，`SocketAdapter` 通过 Unix socket 联系常驻桥接；两者沿用 confirm、status、send、reconcile 契约。socket 目录和文件验证所有者及权限，命令在启动时确定。

Antigravity 实现在官方 Sidecar 环境中调用 `agentapi`，显式绑定 project、conversation 和 workspace。当前接口不能确认忙闲或核对历史发送结果，适配器返回 unknown；本机沿用用户授权的专用空闲工作者、一次一任务测试配置。其他宿主需要相应桥接实现和单独验收。

### 11. files.py：受控文件写入

[查看源码](../src/local_ai_connector/files.py)

| 函数 | 具体职责 |
| --- | --- |
| `write_lease()` | 对工作区根目录的固定锁文件取得非阻塞独占锁；已有占用则报 `file_busy` |
| `parent_fd()` | 校验可见相对路径，逐级打开目录，拒绝越界路径、隐藏路径和符号链接路径 |
| `snapshot_at()` | 只读取普通、单硬链接文件，检查大小并计算 SHA-256 内容摘要 |
| `snapshot()` | 对外提供文件版本；文件不存在时返回空值 |
| `write_file()` | 取得锁、比较预期版本、写临时文件、再次检查版本、原子替换并同步落盘 |

已有文件会保留权限位；新文件使用私有权限。当前内容上限为 10 MiB。

锁的粒度是整个工作区根目录：同一根目录中通过该接口的写入串行进行。版本检查比较的是文件内容摘要。外部编辑器和桌面 AI 原生写入不会自动取得这个锁，最后一次检查与替换之间仍可能遇到外部写入竞态。并行代码修改目前优先通过独立 worktree 分开进行。

### 12. supervisor.py：模型配置与异常解释

[查看源码](../src/local_ai_connector/supervisor.py)

`validate_config()` 检查服务地址和模型名称：云端地址要求 HTTPS，地址中不能嵌入用户名、密码或查询参数。`configure()` 在终端收集配置，密钥不回显，以私有权限原子保存。

`advise()` 调用兼容 Chat Completions 的 `/chat/completions` 接口，把事件作为输入，要求返回 JSON。支持可选密钥及 `reasoning_effort="none"`。`Advice` 限制返回说明长度和建议类别：

| 建议值 | 含义 |
| --- | --- |
| `retry` | 在有效条件内复用原编号重试 |
| `clarify` | 先澄清意图或对象 |
| `wait` | 等待仍在进行的事件 |
| `stop` | 停止当前操作 |
| `inspect` | 先核查版本、身份或记录 |

上游返回 HTTP 错误时，只保留状态码，避免把可能含有密钥的错误正文展示出来。结构不符合要求的模型回答会明确报错。`test_model()` 使用一个文件占用样本验证接口连通性。

当前模型由用户点击或命令触发，未接入自动审批、自动写文件或自动修复循环。结构校验能限制返回格式，不能保证建议判断正确。

### 13. ui.swift：用户操作窗口

[查看源码](../src/local_ai_connector/ui.swift)

`ConnectorWindow` 使用 macOS AppKit 构建原生窗口，采用白底、黑色宋体。上半区显示通道选择、求助原文、截止时间及批准／拒绝／撤销按钮；下半区提供模型地址、名称、密钥、关闭思考、测试连接和解释最近异常。

| 方法组 | 做什么 |
| --- | --- |
| `applicationDidFinishLaunching()` | 读取配置、创建界面、恢复模型设置、启动刷新定时器 |
| `label()`、`button()` | 统一控件字体和基础样式 |
| `admin()` | 使用管理员凭据请求 `/admin` |
| `refresh()`、`selectChannel()` | 每 2 秒更新状态，保留选择并显示求助原文 |
| `decide()` 及三个操作按钮 | 提交批准、拒绝或撤销，由服务判断是否合法 |
| `saveModel()` | 检查输入并保存模型配置，设置文件权限为 0600 |
| `runModel()` | 在后台启动 Python 模型命令，读取结果并展示中文说明和建议 |
| 退出处理 | 关闭最后一个窗口时退出界面进程 |

界面每 2 秒刷新是普通程序请求，与 AI 的长等待分开。关闭窗口后，本机消息服务仍由单独的 `serve` 进程运行。当前界面主要显示结果，模型命令失败时给出通用排查提示，详细诊断仍需命令行。

### 14. __init__.py：Python 包标识

[查看源码](../src/local_ai_connector/__init__.py)

目前仅包含包说明，没有业务逻辑或启动副作用。

## 四、数据库和运行文件

### state.sqlite3：消息与审批

| 表 | 保存内容 | 对应目的 |
| --- | --- | --- |
| `peers` | 端点名称、凭据 SHA-256 摘要 | 识别来访端点 |
| `channels` | 双方、原始求助、状态、批准时间、总期限、空闲期限和求助去重编号 | 保存一次交流的授权与生命周期 |
| `messages` | 顺序号、消息编号、双方、类型、正文、回复对象、完成标记和去重编号 | 关联问答、回放和去重 |
| `incidents` | 端点、错误码、说明和时间 | 支持状态查询与模型解释 |
| `approval_decisions` | 通道、host_elicitation 来源、请求端点、决定和时间 | 宿主审批审计及幂等性 |

消息数据库开启外键和 WAL 日志模式。由单个服务进程持有数据库和等待通知对象，服务目录锁防止同时启动两个实例。已有数据库保留 approved_at 字段兼容处理，新增表按需创建；尚无统一的版本迁移系统或历史记录自动清理机制。

### wakeup.sqlite3：派发与读取证据

| 表 | 保存内容 |
| --- | --- |
| `dispatches` | 端点、明确目标、游标、当前阶段、尝试次数及时间 |
| `dispatch_messages` | 消息 ID 与派发的对应关系，防止同一消息重复覆盖 |
| `dispatch_events` | 有序阶段记录及原因，包括晚到确认 |
| `delivered_messages` | 各端点实际读取过的消息 ID |

### 配置与运行文件

| 文件 | 用途 |
| --- | --- |
| `server.json` | 地址、端口、管理员和已登记端点凭据、能力资料、独立 approval_tokens |
| 各端点 `.json` | 地址、端点凭据、身份模式、可选任务绑定及 requester 工具／审批配置 |
| `state.sqlite3` 及 SQLite 附属文件 | 通道、消息、异常持久化 |
| `wakeup.json`、`wakeup.sqlite3` | 可选派发配置、状态和事件；正文仍在消息库 |
| `server.lock`、端点 `.lock` | 服务独占及配置更新锁 |
| `model.json` | 用户配置的模型地址、名称、可选密钥和思考设置 |
| `Local AI Connector.app` | 本机编译生成的配置窗口 |

运行状态与源码分开管理，默认数据目录是 `.connector`。数据库只存端点凭据摘要，但配置文件保存原始凭据；文件权限保护不等于加密存储。模型权重由 Ollama 等推理服务管理。

### 本机维护部署

- 维护源码和 Python 环境：`/path/to/local-ai-connector` 及其 `.venv`。
- 本机数据：`~/.local/share/local-ai-connector`；服务地址 `http://127.0.0.1:38475`。
- 最近已认证 MCP 状态查询成功；Antigravity Sidecar 负责 socket 桥接，配置目标的只读确认仍正常。
- Codex requester 配置使用两个工具、660 秒宿主工具超时。Antigravity 专用项目预授权 `mcp(local_ai_connector/connector_receive)`、`mcp(local_ai_connector/connector_send)`；文件和命令继续服从该宿主权限。
- 本机共享服务已由当前用户的 LaunchAgent 托管，启用 RunAtLoad／KeepAlive。本轮从 Codex 手动进程接管并验证 SIGTERM 后自动恢复；现有 MCP 直接重新连接，10 条通道、16 条消息、7 条批准记录保留，两库检查正常。重新登录／重启电脑后的自动加载尚未实测，Antigravity sidecar 继续由其宿主管理。
- 源码升级后仍需重启服务，并让宿主重新连接 stdio MCP，避免常驻模块缓存旧实现。当前维护源码与解释器使用绝对路径，移动或删除它们前应更新后台配置。
- 私有配置备份位于数据目录内的 `deployment-before-chat-delegation-20260921`；早期替换条目另存 `installation-previous.json`。

## 五、一次求助怎样经过这些代码

当前 requester 委派流程如下。短任务的确认、唤醒和真实回复已实测；追问续接路径已通过程序回归及 G 轮真人复测。

1. 请求方调用 `connector_status`，根据 workers 中的 ID 和能力选择目标。
2. 调用 `connector_delegate(target, message, request_key)`，核心创建 pending 通道；重复同一操作复用原参数。
3. `delegate()` 在聊天中展示目标与原文。用户确认后，可信前端通过 `/decision` 提交；批准、审批审计和首次问题写入同一事务。
4. 派发器确认目标与授权，先记录意图再调用适配器唤醒；如果工作者已主动 receive，实际读取证据可避免多余唤醒。
5. 工作者用 `connector_receive` 取得原问题。任务通信工具可在宿主项目中预授权，文件和命令仍按工作者宿主权限执行。
6. 需要补充时，工作者通过 `connector_send(kind="question", reply_to=原问题ID)` 追问。请求方收到 input_required 后，以原参数加 reply_to、reply 续答；原任务未完成的未读补充可触发新派发。
7. 工作者通过 `connector_send(kind="answer", reply_to=原问题ID)` 返回最终答案。委派工具确认仍无未解问题后调用 finish，并把真实答案返回原聊天。
8. 超时或断开后恢复原任务；授权有效期内可重读已完成结果。到期或撤销后结果接口不再返回正文，已保存的本地归档保留供检查。

peer 客户端仍可自行组合 request_help、receive、send、finish，并由原生控制窗口或 CLI 批准；它与 requester 共用核心消息和授权规则。

当前默认总期限为 3600 秒，**从创建求助时开始计算**；待批准期间也会消耗期限。空闲期限默认 120 秒，需显式 `finish()` 才开始；并不要求双方各发送一次结束确认。

聊天中的任务确认授予本通道通信权限。工作者自身的工具、文件和命令权限由其宿主控制；当前专用 Antigravity 项目预授权的只有 receive、send 两个通信工具。

## 六、测试和辅助文件各自负责什么

### 自动化测试

最近一次全套运行（2026-09-22）为 **361 passed，48.10 秒**，参数化测试按展开用例计数。新版 MCP 修复新增 19 项，覆盖双向确认、篡改与错配状态、撤销与到期、重启恢复和同通道追问。此前 342 项为 2026-09-21 的基线。在此前 274 项基础上，新增 11 项真实 stdio 双向契约、20 项实际确认文案、28 项私有配置迁移和 9 项模拟部署／兼容验证。早期 203 项基线中的 24 项续接回归及失败复现记录继续保留在验证文件中。

在维护源码目录运行：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider --tb=short
```

| 测试文件 | 主要验证 |
| --- | --- |
| `test_core.py` | 批准、关联问答、空闲计时、去重、撤销、取消和重启回放 |
| `test_http.py` | 认证、管理权限、来源与 Host、严格参数、原文可见性及异常隔离 |
| `test_mcp.py` | stdio／HTTP 混合问答、握手、取消、重接、服务重启及端点隔离 |
| `test_cli.py` | 初始化、配置导出、私有权限、无效输入及已有配置保护 |
| `test_identity.py`、`test_registry.py` | 原生／配置式身份、能力目录、登记与回滚、运行中修改拒绝 |
| `test_delegation.py` | 确认与同调用回传、拒绝／取消、等待预算、超时恢复、宿主 schema 与元数据 |
| `test_delegation_approval.py` | 独立审批凭据、跨端点拒绝、审计事务、到期撤销及原问题结果关联 |
| `test_delegation_followup.py` | 幂等补充、分页追问、并发状态变化、整体截止时间及关闭后到期回放 |
| `test_delegation_mcp.py` | 真实 stdio 子进程中的 form 确认与单次调用结果；确认方和工作者由测试程序扮演 |
| `test_participant_mcp.py`、`test_delegation_locale.py` | 两个双向端点的批准／拒绝／取消、追问续答、凭据要求，以及实际英文确认文案 |
| `test_chat_approval_migration.py`、`test_chat_approval_deployment.py` | 私有配置迁移、失败回滚／恢复副本、模拟服务维护及原登记兼容 |
| `test_wakeup.py` | 持久意图、重试／取消恢复、忙闲门槛、迟到确认、读取证据及可选集成 |
| `test_wakeup_continuation.py` | 多轮追问续接、重启与重读去重、首次消费持久计时及读取竞态 |
| `test_adapter_socket.py` | socket 桥接、权限与断开时的发送歧义 |
| `test_files.py`、`test_worktrees.py` | 真实进程竞争、版本与路径保护、临时 worktree 隔离 |
| `test_supervisor.py` | 模型 HTTP 错误、缺失结果和非法建议类别 |

这些测试验证对应程序契约；真实宿主的忙闲、模型行为和 UI 决定仍需独立验收，不能由用例总数推断。

### 模型评估与证据

| 文件 | 作用 |
| --- | --- |
| `examples/evaluate_supervisor.py` | 读取模型配置，逐条发送合成事件，记录解释、建议、耗时和类别是否符合预设 |
| `tests/model_cases.json` | 8 条中文样本，涵盖占用、版本冲突、到期、错配、不明确请求、等待、投递未确认及消息中夹带指令 |
| `docs/model-validation.json` | 已有本地模型实测输出 |
| `docs/desktop-wait-evidence.json` | ZCode 单次等待的宿主日志摘录 |
| `docs/VALIDATION.md` | 汇总已通过验证、未完成验收、兼容问题和边界 |
| `docs/GENERALIZATION.md` | 当前通用结构、审批选型、本机部署及真人验收结论 |
| `deployment/verification.json` | 通用服务迁移与程序 MCP 往返证据 |
| `deployment/live-verification-20260921.json` | 早期真实模型 MCP 委派记录 |
| `deployment/chat-delegation-verification-20260921.json` | 一次确认、真实恢复、真人追问续接及最新 203 项测试与源码摘要；重点看 single_confirmation_acceptance、robustness_followup、clarification_live_acceptance |
| `deployment/enable-background-service.command` | 用户终端中执行的后台接管与自动恢复验证；当前安装与目标进程检查通过后才切换 |
| `deployment/service-autostart-verification-20260921.json` | LaunchAgent 接管、SIGTERM 后自动恢复、MCP 重连、数据连续性及未验证边界 |
| `deployment/claude-code-verification-20260921.json` | Claude Desktop Code 的空闲激活、英文工具加载、会话恢复、274 项测试及真实 MCP 往返 |
| `deployment/claude-gemini-peer-verification-20260921.json` | Claude 与 Antigravity 直接通信的英文原文、批准路径、消息关联、实际读取与双方界面核对 |
| `deployment/participant-approval-verification-20260921.json` | 双向批准的源码摘要、361 项测试、Claude/Gemini 经 MCP 参与开发的记录及部署验收阶段 |
| `deployment/enable-peer-chat-approval.command` | 为本机两个工作者启用各自聊天批准，保留身份与唤醒配置并恢复服务 |
| `deployment/register-claude-code.command` | 登记或更新指定 Claude 会话绑定，保留现有端点并验证共享服务恢复 |

上表中 `deployment/` 下的 JSON／JSONL 验证记录含本机路径与会话标识，仅保存在维护者本机，已由 `.gitignore` 排除，不随源码发布。

模型评估程序只把“建议类别是否属于预设集合”作为接受条件，没有自动评价解释是否充分或实用。样本结果不能解释为一般准确率。

### 构建和项目说明

| 文件 | 作用 |
| --- | --- |
| `pyproject.toml` | 包名、版本、Python 要求、依赖、命令入口、构建配置和测试配置 |
| `uv.lock` | 固定依赖版本与包哈希，支持复现安装 |
| `.python-version` | 指定开发使用 Python 3.13 |
| `.gitignore` | 排除环境、缓存、运行数据、桌面客户端配置，以及维护者本机的验证记录和已退役实验脚本 |
| `README.md`、`README.zh-CN.md` | 英文主说明与中文辅助说明：安装启动、客户端接入、协作流程、文件操作、模型配置、已知限制 |
| `CONTRIBUTING.md`、`SECURITY.md` | 协作规则（含不可破坏的安全约束）与私下报告安全问题的方式 |
| `docs/ARCHITECTURE.md` | 面向贡献者的英文架构与代码地图 |
| `.github/workflows/tests.yml` | 在 macOS runner 上安装锁定依赖并运行全部测试 |
| `LICENSE` | 主体源码的 MIT 许可证 |
| `NOTICE.md` | 第三方依赖许可证、参考项目、调研依据与采用范围 |

主要直接依赖是官方 MCP SDK、Starlette、Uvicorn 和 HTTPX。SQLite、异步等待、文件锁和哈希使用 Python 标准库；窗口使用 macOS AppKit。Pydantic 由现有依赖链安装并被源码直接用于校验，当前未单独列为顶层依赖。

## 七、实测结论与当前限制

| 范围 | 已有证据与限制 |
| --- | --- |
| 一次确认短任务 | E 轮用户确认只在 Codex 批准一次；Gemini 约 10.95 秒回传 391，双方可查，派发次数为 1 |
| 完成结果重读 | E 轮同任务重读约 9 毫秒，批准、消息和唤醒数量均不增加 |
| 执行等待恢复 | F 轮首次等待 10 秒返回 running，复用原任务约 11 毫秒返回 completed，总唤醒为 1 |
| 真人拒绝／取消 | 程序路径有覆盖；F 原计划验证拒绝，实际审计是 accept 且已投递，因此不计为拒绝验收通过，用户实际点击选择尚待确认 |
| 追问续接 | 17 项专用程序回归通过；G 轮在当前 Codex 聊天中实测，1 次批准、4 条消息、2 次必要唤醒，补充调用约 21.73 秒返回；两端与数据库正文一致 |
| 宿主差异 | Antigravity 和 Claude Desktop Code 的专用空闲单工作者已通过任务往返；忙碌、并发、其他宿主及 ChatGPT Work 的 requester 确认仍需单独验证 |
| 部署与平台 | macOS LaunchAgent 已接管，退出后自动恢复、MCP 重连和数据连续性通过；重新登录后的自动加载尚未实测，Windows 仍受 Unix 文件锁限制 |
| 文件与异常辅助 | 文件保护只覆盖专用接口；模型解释未接入自动批准，历史小模型建议质量不足以承担自主决定 |

早期 ZCode 80 秒长等待（实际约 81.047 秒）、桌面求和与原生身份绑定记录继续保存在 VALIDATION.md。它们证明对应版本的接入与通信，不证明 ZCode 空闲激活。

## 八、后续维护应从哪里入手

| 出现的问题 | 先检查的位置 |
| --- | --- |
| 桌面看不到工具或等待提前结束 | 客户端配置、宿主超时、`cli.py` 配置输出 |
| 提示任务身份缺失或冲突 | `identity.py`、宿主元数据、端点绑定文件 |
| HTTP 认证或参数错误 | `server.py`、端点配置、`client.py` |
| 聊天确认未出现或瞬间取消 | `delegation.py` 的能力协商、schema、宿主返回元数据；`mcp_server.py` 的 requester 配置 |
| 任务确认无法提交或审批范围不符 | `client.py` 的独立凭据、`server.py /decision`、`approval_decisions` |
| 未唤醒、重复唤醒或状态停滞 | `wakeup.py`、wakeup-status 事件、目标绑定与宿主适配器 |
| 补充后工作者没有继续 | `delegation.py` 的 reply_to／幂等键，`wakeup.py` 的候选、实际读取与计时，续接回归 |
| 目标列表或身份字段不正确 | `registry.py`、server.json 的 peer_profiles、端点 metadata 映射 |
| 回复错配、重复消息、期限或结束行为不符预期 | `core.py` 和对应核心测试 |
| 文件占用、版本冲突或路径拒绝 | `files.py` 和调用方的版本读取步骤 |
| 窗口显示或操作异常 | `ui.swift`；确认运行实例对应最新构建 |
| 模型连接或返回格式异常 | `supervisor.py`、服务兼容性和模型配置 |
| 模型解释内容不可靠 | 样本和真实输出；不应靠放宽程序权限修正模型判断 |

Claude Code 的登记、MCP 工具加载及真实委派回传已完成。其真人追问续接、真人拒绝与取消、重新登录后的服务加载、长任务和忙碌状态继续作为独立验收项。

**项目结论：** 核心通信、聊天内委派和可替换唤醒已形成可运行流程；Codex → Antigravity 的短任务一次确认及真人追问续接已实测，Codex → Claude Desktop Code 的英文任务往返也已实测。当前以单个专用空闲工作者为验证范围。

## 待实现：发起方确认后的项目会话打开与恢复（2026-09-28）

用户要求连接器能够在指定项目路径自动打开对话，相关授权在发起方聊天中完成。当前手动补救已获明确授权：Claude 工作者从失效原会话绑定到 “Local AI Connector setup (fork)”（00000000-0000-0000-0000-000000000002），核验收件接口后已写入配置，尚待用户重启后台服务加载。仍沿用已批准通道 00000000-0000-0000-0000-000000000003，避免重复任务。

成功标准：发起方展示并确认目标应用、绝对项目路径、恢复或新建动作及任务内容；在该范围内打开会话，等待宿主报告实际会话身份和收件地址，再绑定并投递一次。拒绝不打开、不投递；启动失败给出明确结果；重新打开后的临时地址变更不能导致任务丢失或重复。现有模型、文件和命令权限继续由宿主管理。

初步接口核查：[Claude Desktop 官方深链接](https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link)提供 claude://code/new 的 folder 参数，但明确要求目标端再次确认文件夹；它不单独满足发起方一次批准。另一个 [claude-cli:// 接口](https://code.claude.com/docs/en/deep-links)启动的是终端会话且只预填提示，不能替代桌面项目对话。下一步需要核查桌面已有会话恢复入口和可支持的授权传递方式，再做最小验证；本轮未实现自动打开项目功能。


## Claude 批准方案并行调研（2026-09-28，待双方讨论）

用户要求 Codex 与 Claude 分别调查、汇总讨论后决定方案，当前未修改实现。已通过 MCP 创建只读调研请求 `claude-consent-design-20260928-a`，通道 `00000000-0000-0000-0000-000000000004`；两次调用均返回 approval_timeout，尚未获得 Claude 回复，不能写作双方共识。

Codex 新证据：Claude 2.9939.2 的 SDK 已将 `requires_user_interaction` 映射为 `requiresUserInteraction`，应用自己的文件夹授权、删除授权及浏览器授权工具也使用该元数据。旧 SDK issue #415 的字段丢失问题不能直接套用当前安装包。官方文档说明该标记强制逐次许可，但实际 Desktop 渲染、任务完整显示、拒绝/取消、已存在 allow rule 和恢复行为仍须隔离验证。

候选路径：标准 elicitation 保持默认；另行验证显式配置的宿主强制工具许可适配。不能仅凭 clientInfo、模型参数、一次普通工具成功或毫秒级拒绝推导授权。若采用工具许可路径，还需设计原文/目标绑定、独立审计来源（当前代码固定为 host_elicitation）、授权到期和重试去重；每次等待重试都强制弹框也会破坏一次确认体验，需在讨论中解决。当前仅为候选，未决定实现。

来源：[官方强制工具许可](https://code.claude.com/docs/en/mcp#require-approval-for-a-specific-tool)、[SDK 用户输入](https://code.claude.com/docs/en/agent-sdk/user-input)、[旧 SDK 字段丢失报告](https://github.com/anthropics/claude-agent-sdk-typescript/issues/415)。


## Claude 并行调研与讨论结论（2026-09-28）

已通过真实 MCP 完成两轮交流：初审通道 `00000000-0000-0000-0000-000000000004`，回答 `00000000-0000-0000-0000-000000000005`；复审通道 `00000000-0000-0000-0000-000000000006`，回答 `00000000-0000-0000-0000-000000000007`。Claude 查阅官方文档后提交建议，Codex 核对现有代码和本机 SDK，再反馈异议；Claude 已确认修正。先前调研请求超时的状态已由本轮实际完成记录取代。

确定下一步为**独立、无凭据探针验证**，尚不修改正式实现：只验证官方 `anthropic/requiresUserInteraction` 对外部 MCP 工具的原生批准效果；没有真实委派或通道批准权限。优先在明确选定的隔离 Claude 项目和当前模式中检查完整目标/多行任务是否可查看、接受/拒绝/取消、同参再次调用是否再提示。记录 initialize 能力及协议、调用编号、工具到达记录，并结合用户实际操作确认。未收到调用本身不能证明用户拒绝。其他权限模式、allow rule、重启实验须另行明确测试范围，不改变现有工作会话的安全设置。

若探针通过，候选实现为显式配置的审批传输方式：默认继续使用标准 elicitation；经用户选择的 Claude 端点可使用强制工具许可，独立记录审批来源。只在首次委派工具上要求批准，任务获批后通过现有 participant receive/send/finish 续接，避免为了等待反复调用强制批准工具。通道 active 仅证明已授权、初始消息已入库，不应称为工作者已经收到。批准前中断或重新调用发起工具仍可能再次要求确认，应如实说明。生产启用前再做无害任务的真人验证。

讨论中排除的错误判断：现有代码已有 elicitation 能力检查，不能把另一 Desktop 路径的报告套用成这里缺少检查；拒绝响应保留宿主原值，不能用耗时自动改写；clientInfo 仅是诊断标签；工具许可路径没有 elicitation 的签名回执，不把它记成同等的用户确认凭证。需信任经明确配置的宿主许可处理，不能由模型参数、宿主名称或运行时失败自动切换。

本轮仅更新研究记录，未修改实现、凭据或宿主权限配置。详细实现报告待实际验证和实现后提交。


## Claude 原生工具批准实测（2026-09-28）

独立项目 `/path/to/claude-permission-probe`、会话 `00000000-0000-0000-0000-000000000008`，Claude 2.9939.2 / Auto 模式。无凭据探针公布官方强制交互标记，不调用连接器服务或其他模型。

- 批准：用户确认完整任务可见并点击单次批准；探针执行一次。
- 拒绝：Codex 观察到原生卡片显示目标和任务、只有 Deny / Allow once；用户点击 Deny；服务端未收到该调用。
- 同参重试：再次显示同一批准框，先前单次批准没有自动沿用；没有新增执行。
- 中止：用户反馈只有 accept 和 deny，没有找到 Stop。取消/中止路径未验证，不记为通过。

最终日志仅有一次执行。当前 Auto 模式下的单次批准、拒绝、重复调用再次确认有真人与日志证据；其他模式、已存在 allow rule、极长文本及重启仍未测。可以据此继续设计显式配置的宿主许可适配，但不能声称完整生产授权已经通过。正式实现和原 Claude 工作者绑定未改动。

临时项目 MCP 配置已移除，保留可复用探针源码及验证记录。临时会话当前空闲；进程退出未核验（沙箱禁止进程检查，UI 归档操作遇到用户切换，未确认归档）。证据见 `deployment/claude-permission-probe-20260928-summary.json` 和同名 JSONL。


## 原生工具许可接入实现（2026-09-28，待部署验收）

新增端点配置 `approval_transport`：默认 `elicitation`，显式 `host_tool_permission` 仅允许具有私有审批凭据的 participant。仅后者的 delegate 公布官方逐次交互标记；不从模型名、clientInfo 或表单失败推断切换。Claude 测试项目已验证当前 Auto 模式下的批准和拒绝；这仍是对配置宿主的信任，不是签名真人回执。

该路径一次调用创建/批准通道后立即返回 active 和 channel；active 只表示原始消息入库。后续 receive/send/finish 复用通道，不重新请求任务批准。重复调用 delegate 仍会触发宿主许可，内部去重防止重复投递；改动目标/正文拒绝，已拒绝/撤销/过期任务不复活。已关闭任务重读走现有 result，保留原授权期限。后续模型补充回复受通道权限约束，不另取用户批准，与原有续答语义一致。

`/decision` 新增受限 source 值，旧客户端省略仍记 host_elicitation；新路径记 host_tool_permission，只有原有审批专用凭据可提交，普通端点凭据、跨端点与来源重写均被拒绝。未改变模型可见工具参数以接收 approved 标记。

提供 `deployment/enable-claude-tool-approval.command`：仅更新已登记 Claude participant 的 approval_transport，验证服务归属和当前无未结束任务，然后重启既有服务并验证新审批来源受支持。不修改工作者绑定、Gemini/Codex 配置或凭据。运行前暂不发起新任务。脚本已做 shell/Python 语法检查；真实部署尚未执行。后台服务管理需要在用户终端完成（当前沙箱不能管理 launchd）。

本轮最终自动测试：405 passed，64.87 秒（原384项加21项）。覆盖新协议/旧协议的显式宿主许可、无重复投递、正文替换拒绝、通道续接、来源审计、凭据隔离、过期撤销、审计事务回滚、关闭结果重读及授权到期。另有部署脚本语法检查通过。自动测试模拟宿主门禁，不冒充真人确认测试；真实部署与 Claude → Gemini 回传验收仍待执行。


## 原生许可部署与真实验收进度（2026-09-28）

用户已运行 enable-claude-tool-approval.command，输出确认配置更新、服务重启及独立审批来源验证成功。Claude 应用重启后恢复同一个绑定会话 00000000-0000-0000-0000-000000000002。首次消息在 Sending 停留约两分钟，随后正常发现 gemini 工作者并进入原生批准框。

真实测试 request_key 为 claude-native-live-20260928-a，任务计算 23 × 31，标记 TEST-CLAUDE-NATIVE-20260928-A。已观察批准框包含完整目标、原文和 request_key，提供 Deny / Allow once，无 Confirmed 复选框。当前等待用户单次批准，尚不能记作通信成功。


真实通信已返回：通道 `00000000-0000-0000-0000-000000000009`，一条 host_tool_permission / accept 审计，一条 Claude 问题及一条关联 Gemini 回答（713，标记 TEST-CLAUDE-NATIVE-20260928-A）。Claude 已在原会话展示原样回答，并调用 finish 开始闲置关闭计时；没有为了等待重复调用 delegate。用户是否在其他应用另点批准尚未确认，不能据此宣称完整单次点击体验已经验收。证据：deployment/claude-native-live-20260928-a.json。


用户补充确认：本次 Claude → Gemini 实测仅在 Claude 点击一次，未在 Antigravity 或 Connector 额外批准。因此本次短任务的一次批准体验通过；macOS 文件夹访问授权属于另一个系统权限事项。

## Codex Desktop shared-server experiment (2026-09-28)

The installed desktop currently initializes its local app-server over stdio. A user-run enable/rollback script is prepared at `deployment/codex-desktop-shared-server-experiment.command`. The daemon settings implementation stores `remoteControlEnabled` in `~/.codex/app-server-daemon/settings.json`, defaults it to false when absent, and errors on malformed settings ([official source](https://raw.githubusercontent.com/openai/codex/main/codex-rs/app-server-daemon/src/settings.rs)). That local settings file was absent before the experiment. The script checks it read-only, rejects true/malformed/unreadable settings and non-default `CODEX_HOME`, and requires the daemon start lifecycle response to be `started` before recording socket ownership; `alreadyRunning` is refused. Shell syntax passes. Runtime switching remains unverified: any eventual desktop test still needs websocket transport evidence and read-only confirmation that the dedicated test thread is loaded.

Experiment outcome: the user ran enable; the managed daemon reported version `0.158.0-alpha.2.1`, matching the bundled CLI. The new Desktop log at 18:18Z shows `transport=stdio` and successful stdio initialization. No queue message was sent. The fallback cause is unverified, so shared-mode execution is a no-go. Owner rollback is pending; cleanup has not been confirmed.

## Codex Desktop shared-server experiment findings (2026-09-29)

Static inspection of the installed ChatGPT app bundle explains the stdio selection on this local path: `application-network-startup-CY4ZWOz-.js` enters the shared-daemon WebSocket branch only when `await options.getConfigOverrides()` is empty. In `main-C5425b_s.js`, the local-host configuration builder always appends a `plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=...` override, including when its value is `false`; therefore this installed version supplies a nonempty override and selects stdio. The 18:18Z Desktop log confirms that transport. This is a finding about the inspected app version and local-host path, not a claim about later versions or other paths.

Rollback was verified: the experiment state file is absent, daemon version lookup fails with socket `ENOENT`, and the latest Desktop log (2026-09-29 13:17Z) records successful stdio initialization. No test message was queued. Do not repeat the shared-flag experiment or suppress tool configuration to force the WebSocket branch; that would bypass the normal configuration path.

## Codex Desktop IPC and ingress findings (2026-09-29)

Installed Desktop source exposes `thread-owner-discovery` v1 and `thread-follower-start-turn` v2. The local IPC socket belongs to the current user and has owner-only permissions. A bounded read-only probe was implemented with fake-socket tests (6 passed); the attempted live connect was blocked by sandbox `EPERM` before sending requests, so live IPC success is unverified. No production configuration changed.

The installed `codex://threads/new` deep link creates a composer prefilled through `prefillPrompt`; it does not submit a turn. The inspected `codex-web` implementation creates new threads through its own app-server. No license was verified and no source was copied. Its Desktop IPC route only targets existing threads.

User authorized the fallback ingress design: an external request wakes a dedicated existing Codex thread, whose agent calls native `create_thread`. A dedicated chat (`00000000-0000-0000-0000-000000000010`) was started with `gpt-6-luna` at low reasoning and replied ready with the tool available in 6.047 seconds. This confirms chat readiness only; it is not an IPC test or cost benchmark. `list_projects` is currently empty, so creation for an arbitrary project path remains unverified. User terminal execution of the probe is pending because the sandbox could not connect to Desktop IPC.

The user subsequently ran the read-only probe in Terminal and reported both `ok` and `owner_found` as true. This establishes user-reported connectivity and owner discovery for the pinned test chat. It does not yet establish turn submission, new-thread creation, or an external MCP round trip. The next isolated test submits one English arithmetic prompt to that existing chat and checks its visible response, retaining a durable send-intent record to prevent accidental repeats.

Live test A returned `Desktop IPC returned an unexpected message`. Its intent exists, no acknowledgment was recorded, and the dedicated chat showed no new test turn when inspected. Because A recorded intent before handshake and did not retain the unexpected message type, its send stage is unknown; A remains unreplayed. Installed IPC handlers confirm that discovery requests and inbound requests are legitimate interleaved messages. The probe now answers discovery negatively and rejects inbound requests without executing them, preserving its deadline and frame limits. This repairs a demonstrated protocol coverage gap, but the exact original message type remains unverified. A fresh B test uses separate records; it records intent immediately before start-turn and refuses duplicates before connecting. The 24 targeted fake-transport tests pass, including interleaved requests, malformed IDs, acknowledgment identity/shape, pre-send failure, duplicate invocation, and ambiguous timeout. B has not yet been run live.

Live B subsequently passed. The user reported the expected outcome; the saved acknowledgment names turn `00000000-0000-0000-0000-000000000011`, and a read of the pinned Desktop chat confirms that same completed turn with `TEST-CODEX-IPC-20260929-B 893`. The turn duration was 2.429 seconds, not a complete end-to-end latency or token-cost measurement. This establishes IPC-triggered execution in the existing Desktop chat. New-chat creation, project binding, and external MCP task/reply integration remain separate acceptance steps.

The separate native-creation capability test was requested through `send_message_to_thread`, not IPC, with an explicit single projectless chat, GPT-6 Luna/low, and marker `TEST-CODEX-CREATE-20260929-C` (53 × 17). Ingress turn `00000000-0000-0000-0000-000000000012` is waiting on host approval. Creation and the single-approval user experience are not yet verified. The user was asked to inspect and single-approve only the matching creation request; no permission setting was changed.

Native creation subsequently succeeded: child chat `00000000-0000-0000-0000-000000000013`, title `Codex ingress creation test`, returned `TEST-CODEX-CREATE-20260929-C 901`. The user explicitly reported approving once in the initiating chat and again in the Luna chat. Creation capability passed, single-approval experience failed. This creation test used native thread messaging, separate from IPC test B, and does not establish an external MCP end-to-end creation flow.

## Scoped creation approval investigation (2026-09-29)

The reviewed user config enables `codex-app-tools@openai-bundled` without a per-tool approval override; the dedicated ingress workspace has no `.codex/config.toml`. Official [plugin configuration](https://developers.openai.com/plugins/build/plugins) supports per-tool approval modes, but permanently approving `create_thread` would not bind approval to one requested project, prompt or expiry.

A narrower candidate is the official [PermissionRequest hook](https://learn.chatgpt.com/docs/hooks#permissionrequest): MCP tool names and complete arguments are available, and a synchronous hook may allow, deny or leave the normal approval prompt in place. Proposed integration retains the normal approval gate and allows only an exact match to a broker-owned, unexpired, unused authorization for the pinned ingress session and creation arguments. The grant must not be derived from a model's claim of approval. Errors or missing grants must never imply allow. This is a design candidate, not an implemented grant mechanism or a verified installed-version hook path. Non-managed hooks require an initial owner trust review. No hook, approval override, or permission change has been deployed.

## Isolated creation approval prototype (2026-09-29)

Implemented `integrations/codex_desktop/creation_approval.py` and 22 tests. A SQLite transaction consumes an exact session/cwd/tool/full-arguments grant before returning allow; expiry is rechecked after argument comparison. Tests cover changed and extra arguments, type differences, expired/used grants, concurrent consumers, malformed input and missing stores. Combined with the existing Desktop transport tests, 46 tests passed in 0.08s. This is targeted validation, not a rerun of the production suite.

Prepared but did not execute `deployment/enable-codex-creation-hook-probe.command`. It stages only a project-local PermissionRequest hook and an owner-provisioned one-hour, single-use fixture for the D arithmetic creation test (53 × 19, GPT-6 Luna/low). It refuses existing hook/store files, changes no global approval policy, and leaves hook trust to the owner's native Review hooks action. Its remove mode deletes only the exact hook definition and retains evidence. Shell and embedded Python syntax checks passed.

This fixture does not implement broker grant issuance. Owner-only filesystem permissions are not protection against other processes running as the same user; production integration still needs a demonstrated authority boundary. Installed Desktop hook dispatch, one-approval UX, and the external MCP end-to-end path remain unverified. No hook was installed or trusted by the agent, and no D test was sent.

The owner ran the hook installer successfully, but reported no Desktop Review hooks entry. Read-only checks confirm the project-local hooks.json exists and the bundled CLI reports hooks enabled/stable. Neither the ingress path nor its ancestors has a project trust entry in the user configuration. Official hook documentation requires an active trusted project configuration layer; actual Desktop discovery remains unverified. The earlier instruction to expect Review hooks immediately was premature. No trust state was modified and no D creation request was sent during diagnosis.

## Test D manual completion and hook diagnosis (2026-09-29)

The owner reviewed and enabled the exact hook through the bundled CLI. Desktop ingress turn `00000000-0000-0000-0000-000000000014` still requested approval; the owner accepted it. Child `00000000-0000-0000-0000-000000000015` returned `TEST-CODEX-CREATE-20260929-D 1007`. The creation arguments match the fixture, but its consumed_at remains NULL. D must not be replayed merely because the fixture is unused: the creation side effect already happened.

Desktop runtime feedback for this turn reports feature.hooks=true. The Desktop log records the second acceptance at 15:29:00.240Z through mcpServer/elicitation/request. That transport does not prove PermissionRequest hooks are bypassed: current public core source routes MCP approval through ApprovalAction::McpToolCall, including hook_tool_name, before rendering its elicitation UI. The installed plugin server is a thin native-host proxy. Installed settings assets expose Reload hooks, but no evidence yet establishes that this reload refreshes a previously active session's hook runtime.

The fault is narrowed to the Desktop approval/hook integration, not arithmetic or task transport. CLI discovery/trust is proven; successful execution of the checker inside the Desktop turn is not. Existing-session configuration refresh, hook matching and invocation failure remain unresolved alternatives. No broader approval policy was enabled, no further task was sent during diagnosis, and no hook or trust setting was modified.

The owner supplied a Desktop Hooks settings screenshot showing `No hooks found`. Installed settings source computes hook-list cwds from known project roots, selected local-project root paths, and an optional explicit projectRoot route parameter; it does not directly add the cwd of every projectless chat. `list_projects` currently returns an empty list. This supplies a concrete explanation for the empty settings view for the projectless ingress, rather than proving that hooks are unsupported or that its runtime registry is empty. Next diagnostic action is to register/open the existing ingress directory as a Desktop project and inspect its hook listing. Existing-session hook execution still requires separate verification.

After adding the ingress project, the owner reported that Desktop Hooks listed the test hook and it was enabled. Negative test E in existing ingress turn `00000000-0000-0000-0000-000000000016` still requested native approval rather than being rejected by the checker. The owner approved only the initiating chat's delegation, then denied the ingress creation. The tool failed with `user rejected MCP tool call`, duration 0, and no child ID was returned. This confirms manual denial, not hook enforcement. Next diagnostic requires a fresh Desktop runtime/resume to distinguish stale session loading from other integration failures; no positive creation retry is scheduled.

After the owner fully reopened Desktop, negative test F completed in ingress turn `00000000-0000-0000-0000-000000000017`. The exact mismatched create_thread call failed with the checker's custom denial text, `Creation grant is invalid, expired, or already used.`, and returned no child ID. The original grant was still unused and had 596 seconds remaining when checked afterward, so mismatch (rather than expiry or prior consumption) explains this rejection. This demonstrates the hook denial path in the resumed Desktop runtime. The before/after evidence supports stale runtime configuration as the earlier failure mechanism, although project registration and trust changes also preceded restart. User confirmation that no second approval prompt appeared is still pending. Positive grant consumption/single-approval creation remains unverified; completed test D must not be replayed.

The owner confirmed negative test F required approval only in the initiating chat; no second approval appeared in Luna. The automatic-denial acceptance case passed. A separate positive test G is being staged with fresh owner-issued fixture data; D is retained and will not be replayed. This continues to test the native hook path separately from production broker-issued authorization.

Positive test G staging is prepared as `enable-codex-creation-hook-probe.command --test-g`, not executed by the agent. It preserves D evidence and creates a separate one-hour one-use G fixture for 59 × 17, GPT-6 Luna/low. Replacement requires the byte-exact owned D hook definition and checks it again before replacement. Existing G storage is refused. Remove accepts only the exact owned D or G definitions. Shell and embedded Python syntax checks passed; live installation is pending. The changed hook command requires owner review/trust and Desktop reopening before testing.

## Positive hook test G (2026-09-29)

After owner installation, trust/enabling and Desktop reopening, ingress turn `00000000-0000-0000-0000-000000000018` created child `00000000-0000-0000-0000-000000000019` (Codex single approval test G). Child turn `00000000-0000-0000-0000-000000000020` returned `TEST-CODEX-CREATE-20260929-G 1003`. The G grant was durably consumed at Unix time 1790698203.201972. A local checker evaluation of the identical event against that already-consumed grant returned `(False, 'grant-used')`; no host call was made for that repeat check. The live positive path and local replay rejection passed. User confirmation of no second approval UI is pending. This remains an owner-issued fixture and native thread-message test; automatic broker issuance and external MCP-to-Desktop creation are not yet demonstrated together.

The owner confirmed positive test G required exactly one approval in the initiating chat and no additional prompt in Luna. The isolated native Desktop creation test now passes both automatic denial of mismatched input (F) and automatic allowance of an exact one-use grant (G), with a verified child response. Reuse rejection was checked locally, not by attempting another live creation. The remaining implementation step is to issue these scoped grants from the connector's actual owner-approval flow and connect that flow to external inbound delegation; the manual fixture installer is not the production authorization mechanism.


## 三端通用任务续接（2026-09-29）

目标：Codex、Claude Desktop Code、Antigravity 的六个通信方向共用同一任务续接规则。一个已批准任务的追加问题和澄清沿用通道、消息记录与已绑定目标；每轮使用独立去重编号，重试保留该编号。

本轮实现：

- 新增 `connector_continue(channel, message, request_key, timeout_seconds, reply_to, reply)`，覆盖 requester、participant、peer 和 HTTP MCP 工具集。服务端新增 continue 与 round_result 操作，按本轮问题 ID 取答案，避免返回首轮旧答案。执行等待有超时边界，澄清可通过同一工具补答。
- 已批准通道在闲置关闭后，可在原授权截止时间之前重新开始一轮。保持原到期时间；到期、撤销、拒绝时明确失败。完全相同的已完成消息可在仍有效的已关闭通道中重读，不重开通道或延长计时。
- wakeup.sqlite3 新增以 `(channel_id, peer)` 为键的目标绑定；两端分别固定目标。启动时从既有 dispatch 的持久目标与消息关联补齐。首次派发、重试及恢复核对目标，配置漂移时停止派发并记录异常。
- 澄清回复的唤醒依据关联的未完成轮次判断，覆盖首轮完成后的追加轮次。父问题完成后，已排队的澄清唤醒重试结束；普通最终答案维持原回传方式。

选型：复用现有 SQLite、通道和消息去重机制，没有新增依赖。参考 [A2A context/task 生命周期](https://a2a-protocol.org/dev/specification/) 对上下文与执行任务的区分，并核对 [MCP Tasks](https://modelcontextprotocol.github.io/ext-tasks/specification/2026-07-28/tasks.html) 的执行状态能力；这些协议本身不提供本项目所需的桌面会话绑定。本轮以现有通道作为原授权期内的任务上下文。

实现由 GPT-6 Luna 子代理完成，主任务审查并在具备本机端口权限的环境验证。审查修正了双向目标键、后续澄清关联、已关闭消息重试授权、requester 工具暴露等问题。变更主要在 core.py、delegation.py、mcp_server.py、server.py、wakeup.py 及相关测试；使用方法已更新到 README。

验证：

- `.venv/bin/python -m pytest -q --tb=short`：466 passed，63.02 秒。
- 随后仅扩展实际 MCP 客户端的续接测试，运行 `tests/test_participant_mcp.py::test_participants_continue_clarification_without_another_approval`：legacy、modern 两种协议均通过，2 passed，1.58 秒。两项包含在上述测试集合中，扩展后的测试已单独复验，未再次运行整个集合。
- 覆盖六个具名方向及任意名称端点的公共协议测试、闲置关闭后第二轮、去重与重启、目标漂移、撤销/过期、第二轮澄清及父问题完成后停止重试。MCP 集成通过临时本机服务与客户端验证，同一通道返回新轮次答案且批准回调仅发生一次。

当前边界：这些是公共协议和模拟适配器验证。尚未部署，也未进行三个真实应用的六方向续接验收。生产级外部请求到 Codex Desktop 的适配器与 broker 签发创建授权仍待接入；此前 F/G 为独立入口实验。现有工作者仍使用配置中的固定目标，本轮没有增加新任务自动创建桌面对话的生产流程。目标绑定保证派发路由一致性；generic 客户端认证的是端点，不能据此宣称验证了原生会话身份。跨原授权期限的重新授权与上下文延续尚未实现。

下一步：先部署本轮公共续接能力，针对现有已连通路径实测“首轮完成 → 追加问题 → 澄清补答 → 原会话返回”；再完成外部到 Codex 的生产入口，逐项记录六方向的真实会话 ID 与批准次数。


## 续接部署与 Codex → Antigravity 首轮（2026-09-29）

用户执行后台重启后，运行库已出现 channel_targets 表，确认新版唤醒存储已初始化。当前 Codex MCP 客户端仍只暴露旧 delegate/status 两项，已请用户重新连接以加载 connector_continue。真实首轮通道 `00000000-0000-0000-0000-000000000021` 返回 `Round 1: 391 | TEST-CODEX-GEMINI-CONTINUITY-20260929-A`；数据库记录一条 host_elicitation/accept 和原 Antigravity 目标绑定。证据见 deployment/continuity-verification-20260929.json。第二轮仍待执行，尚未单独询问用户是否出现额外批准界面。

用户要求优先完成 Codex 与 Antigravity 双向通信。外部 → Codex 的生产入口仍未登记；正在追踪 Desktop IPC 的忙闲快照与启动行为，不能把首轮回复回传等同于反向主动委派。当前未启用未知状态强制发送，也未变更 Codex、Claude 或 Antigravity 权限。

第二轮已通过：同一通道追加问题后，Gemini 发回 What increment should I add?；回复 Add 9. 后，收到 Round 2: 400，reply_to 关联第二轮问题。总计六条消息、一条 accept 审计，目标绑定保持原 Antigravity conversation_id。第二轮使用新启动的 stdio MCP 客户端调用 connector_continue；它证明已部署 MCP 路径有效，当前聊天缓存刷新后的直接工具体验仍待确认。只读 Codex IPC 快照订阅已成功，专用线程返回 threadRuntimeStatus.type=idle；分页快照 turns=[]，不能用历史数组推断运行状态。


## Antigravity to Codex Desktop preparation (2026-09-29)

Added status_probe.py and bridge.py using the installed Desktop local IPC. They discover the thread owner, subscribe to its snapshot, and validate thread ID, workspace and threadRuntimeStatus. Live read-only confirm/status succeeded for dedicated thread 00000000-0000-0000-0000-000000000010 (gpt-6-luna, low, idle). Dispatch intent is persisted before sending; ambiguous outcomes are not blindly retried; acknowledgments retain turn IDs.

Full Python suite: 491 passed in 64.73 seconds. No live incoming task has been sent through the new bridge yet. The owner-run registration script is under review; its initial missing wakeup binding was identified before deployment.

Limits: this is installed-version internal IPC, not a public compatibility guarantee. Atomic busy rejection between snapshot and start has not been demonstrated, so initial acceptance uses a dedicated idle thread and one task at a time. This stage reuses the existing dedicated thread; automatic new-task conversation provisioning remains separate. Real Antigravity-initiated acceptance and continuation are pending deployment.


## Live Antigravity to Codex acceptance (2026-09-30)

After Desktop reopening, the dedicated thread loaded local_ai_connector_codex_desktop_worker and connector_status returned self=codex_desktop. Gemini initiated channel 00000000-0000-0000-0000-000000000022. The bridge woke the existing Codex thread 00000000-0000-0000-0000-000000000010; turn 00000000-0000-0000-0000-000000000023 received the actual MCP question and returned 713 for 31 * 23. Gemini received that answer and reported it through outer channel 00000000-0000-0000-0000-000000000024. Both applications display the result.

Acceptance remains partial. The owner reports three approvals in Antigravity and two in the Codex test chat. A read-only UI observation confirmed Antigravity's native connector_delegate permission prompt; the exact mapping of all five clicks has not been independently established. Codex executed connector_receive and connector_send, but the thread record alone does not prove which calls displayed prompts. Single-initiator-approval UX has therefore failed this run.

Round 2 stopped with Unknown tool: connector_continue in Antigravity. Its loaded tool list is still the older five-tool set; reloading the MCP client and verifying its actual tool inventory is required before retrying continuation. No replacement channel was opened for round 2.


### Approval diagnosis and owner-led acceptance

The owner requested future acceptance be initiated manually, with the agent limited to observation and diagnosis. No additional live tasks or permission changes were made during this inspection. The nested channel has exactly one approval_decisions row: source=host_elicitation, peer=gemini, decision=accept. Antigravity's native connector_delegate permission prompt was independently observed. Its third reported click cannot yet be mapped conclusively from available evidence.

Codex Desktop logged two accept responses during the inbound turn at 2026-09-30T13:50:22.515Z and 13:50:35.070Z. The turn invoked only connector_receive and connector_send; neither tool implements task elicitation. The dedicated project's new MCP stanza has no per-tool approval rules. These observations identify host tool permission as the extra receiving-side approval layer; the log responses themselves do not name each tool. The existing trusted creation hook matches create_thread only and does not cover these calls.

A separate configuration defect: global Codex enabled_tools contains only connector_status and connector_delegate, so reconnecting alone will not expose connector_continue there. Antigravity's cached five-tool inventory lacks connector_continue. A fresh client launched with the deployment PYTHONPATH explicitly set returns six tools including connector_continue; runtime configuration/loading on Antigravity remains to be reconciled. Do not call this merely a cache failure without checking the active launch configuration.


### Scoped communication permission migration prepared (2026-09-30)

Prepared deployment/migrate-codex-communication-permissions.command. Default execution previews only; --apply backs up and modifies three owner configurations. It exposes and permits connector_continue on the existing Codex requester; permits status/receive/send/finish only on the dedicated Codex worker server; adds exact Antigravity project allows for status/delegate/continue/finish while preserving existing receive/send. The delegate task-confirmation flow is unchanged. No global wildcard, shell/file policy, or creation hook is changed. Existing conflicting explicit permissions are refused. Command/args/endpoint/PYTHONPATH are checked against this installation before mutation.

The actual Antigravity project file had only receive/send allows. Official https://antigravity.google/docs/permissions and https://antigravity.google/docs/mcp confirm unconfigured tools default to Ask and support exact server/tool grants. Its configured MCP launch path and PYTHONPATH match this source; the stale loaded inventory still needs client reload.

Root independently ran eight migration tests (8 passed, 0.01s) and the dry-run against actual configuration: exactly ten changes proposed, no live configuration written. Real one-confirmation behavior remains for owner-led acceptance after application and client reload. Authorization enforces channel identity, original requester and expiry, not semantic on-topic classification of follow-up messages.


### Owner-led latency observation (2026-09-30)

Owner reports one approval and correct content. Channel 00000000-0000-0000-0000-000000000025 currently contains only the 37 * 24 question and 888 answer; no second-round transport is recorded yet. Approval/message at 07:17:38.214 America/Los_Angeles; wake_host_unavailable at 07:17:45.327; Desktop thread/resume success at 07:25:56.113; dispatch at 07:25:57.559; acknowledgment at 07:25:57.680; receive at 07:26:04.728; answer at 07:26:10.227. Approximately 499.35 of 512.01 seconds elapsed before dispatch, with 12.67 seconds from dispatch to answer. Receive/send calls took 8/7 ms. Evidence points to target runtime readiness, not slow MCP payload transfer. Automatic restoration of an unloaded Desktop target remains missing; current gate waits until the existing target is confirmable.

Antigravity also recorded one model API network error before delegation and a three-minute host MCP deadline when delegate used timeout_seconds=180. This compounded status/wait overhead but does not account for the main pre-dispatch gap. No new live test or permissions changes were made during diagnosis.


### Cold Desktop target recovery design review

Owner clarified that after waiting a long time they opened the target chat briefly and switched back. This is consistent with the observed resume followed by immediate dispatch; the exact user-click timestamp was not recorded. Astra Ultra's read-only review of the installed Desktop bundle confirms thread-owner-discovery routes only to an existing owner and performs no cold resume.

The documented codex://threads/<thread-id> deep link opens an existing local conversation (https://learn.chatgpt.com/docs/reference/commands). Installed handlers show that the ordinary local-conversation deep link restores/shows/focuses the primary window and navigates to that chat. OS background launch flags do not suppress the app's own focus behavior. The owner explicitly accepted cold-target-only UI switching/foregrounding. Implementation follows that decision; no recovery side effect or new live task was executed during review.


### Cold Desktop target recovery implementation (2026-09-30)

Following the owner's acceptance of cold-only navigation, GPT-6.1-sol implemented the recovery flow and GPT-6-luna prepared the owner-run deployment command; root reviewed both. A command binding explicitly opts in with `restore: true` (default false). Typed `no_owner`, `socket_missing`, and startup connection failures allow recovery; protocol, permissions, malformed snapshots, wrong target/workspace, and generic failures do not. The bridge rechecks whether the target is already loaded before invoking `/usr/bin/open -b com.openai.codex codex://threads/<registered-id>`. Existing loaded targets do not navigate.

The dispatcher pins the approved target before preparation, checks authority and binding around asynchronous operations, commits a recovery episode before navigation, and coalesces pending same-target messages. Episodes survive daemon restart and prevent repeat navigation for the same batch. The default 60-second recovery window is checked on the existing retry cadence (normally 15 seconds); it is a diagnostic/anti-repeat deadline, not task cancellation. Later verified readiness can still dispatch authorized pending work. Restore success is only navigation acceptance; exact snapshot identity, workspace, idle status and approval state remain required. Existing durable send and ambiguous-outcome handling remain in force.

Validation: root full suite passed 547 tests in 62.43 seconds. After the final same-target coalescing edge fix and new test, root reran all affected wakeup/continuation/Desktop probe/status/bridge and deployment tests: 145 passed in 1.47 seconds. Cases cover hot/cold, busy/unknown, incompatible or unsafe failures, revoke/expiry, target drift, timeout, cancellation/restart, batch coalescing and ambiguous-send protection. The deployment script's success and post-write rollback paths run against temporary configurations and mocked launchd/bridge. An initial full-suite attempt was blocked by socket sandbox permissions; the successful run used granted network permission for isolated local test services.

`deployment/enable-codex-desktop-cold-restore.command` defaults to dry-run; `--apply` backs up wakeup.json, validates the registered worker and launchd process, refuses pending approvals/unanswered work, stops the service before acquiring its lifetime lock, sets only this binding's restore flag, and restarts/verifies a replacement listener. Failure restores the original configuration and attempts service recovery. Root did not run the live deployment or initiate a real inbound task.

Remaining limits: Desktop IPC is tied to the installed application version. Snapshot-to-start busy state is not atomic, so keep using the dedicated worker one task at a time. Revocation while the external restore command is already running cannot atomically cancel UI navigation; authority is rechecked before task dispatch and content retrieval. Cold navigation may switch chats and foreground Codex, as accepted. Actual cold-start latency, first/continuation rounds and settings preservation still require owner-initiated acceptance; observe logs/SQLite during the wait, avoiding read_thread or manual target opening that could affect readiness.


### Owner acceptance after cold-recovery deployment (2026-09-30)

Owner confirmed deployment. Read-only inspection found wakeup enabled, the registered codex_desktop binding set to restore=true, a listener on the configured service port, and the recoveries table initialized. Process command inspection was restricted by the sandbox; no live target probe or task was initiated by root.

Owner initially reported validation passed with exactly one approval, then clarified that explicitly requesting a new Codex conversation still used the existing conversation. Acceptance is partial: communication and reported approval UX passed; new native conversation creation did not. The earlier unqualified completion statement is withdrawn. See the invocation audit below.


### New conversation versus continuation invocation audit (2026-09-30)

Owner supplied the exact instruction: “接下来使用local AI connector另开一个新对话，让codex简单回答一下新上线的dot能做什么”. Read-only Antigravity transcript inspection (steps 268–271, 08:09:22 America/Los_Angeles) shows connector_delegate, request_key=gemini-codex-dot-intro-20260930-a, target=codex_desktop; no connector_continue was called for this request. The message argument retained only the dot question, omitting the explicit new-conversation requirement. Broker created new channel 00000000-0000-0000-0000-000000000026, but wakeup.channel_targets bound it to existing native thread 00000000-0000-0000-0000-000000000010. That is the same native thread used for the preceding calculation channels. The production dispatcher uses binding.target and the deployed adapter opens/sends to an existing ID; new native Codex conversation provisioning is not implemented in this path. Antigravity's final “新对话通道” wording incorrectly presented a new transport channel as satisfying a new native conversation request.

The preceding “追问再加15等于多少” instruction also used connector_delegate (step 265), opening 00000000-0000-0000-0000-000000000027 rather than continuing original channel 00000000-0000-0000-0000-000000000028. Therefore this owner run does not validate connector_continue or approval reuse across same-channel rounds. All three exchanges reached the same Codex native thread; each channel has its own approval record. No new messages, target reads, or live configuration changes were made during this audit.


### 全局会话语义与注册式原生创建（2026-09-30）

用户要求继续修复，并明确同时检查其他方向、尽量共用一套通道代码和注册表。检查确认当前三个宿主适配器都使用固定既有聊天；此问题不能仅通过 Antigravity 提示词修补。

本轮实现：

- 所有 MCP 发起端的 delegate/request_help 必填 conversation_mode=existing|new；模式纳入原任务去重约束和批准内容。completed 仅表示该轮完成，返回 next_round 指向同通道 continue。既有 HTTP open 保持 existing 默认以兼容旧调用。
- server.json.conversations 是端点到原生提供者的注册表；status 报告 conversation_modes。公共 Broker 复用现有批准、消息、撤销、过期、追问、唤醒和去重。未支持 new 的端点在批准、入库和投递前拒绝，不能静默使用旧聊天。
- 新增 codex_ingress 提供者。获批的原始问题经专用入口调用原生 create_thread；随后轮次和澄清补答使用 send_message_to_thread 指向绑定的新 ID。新聊天保留宿主默认模型，除非注册表明确指定；本次安装配置不覆盖模型。当前支持 projectless 目标。
- 原生操作计划在读取获批消息时持久化。入口项目 PermissionRequest 钩子核对会话、cwd、完整参数、授权期限和一次性状态后消费；PostToolUse 读取真实原生 MCP 回执绑定 child thread_id。入口不能通过普通 MCP 提交一个自报 ID，也不能在缺少成功回执时用自己的回答冒充新聊天执行。
- 已消费但结果未知的操作不会自动重建或重发；配置漂移停止路由。钩子对专用入口之外的会话不作决定。新工作者不复制连接器凭据，通信仍经原入口。receive 等待型发起端同样取得原生聊天身份。

采用此前用户已接受且 F/G 已独立验过的原生入口路线，未重试已证实不适配的共享 app-server。官方 hooks 文档说明 PermissionRequest 与 PostToolUse 的输入/结果结构：https://learn.chatgpt.com/docs/hooks 。本机已有成功 create_thread 原始回执证实 content[0].text 中的 threadId、hostId=local；本轮未发起任何原生创建或消息。官方 Claude 深链文档仍要求预填后发送及文件夹确认，不能视为发起方一次批准的完整创建实现：https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link 。Antigravity 现有桥接也未提供已验证的新建实现，因此两者当前声明 existing-only；没有宣称平台不存在其他创建入口。

验证：完整测试 578 passed，65.28 秒。最后将钩子范围收窄到专用入口，并补 CLI 入口的实际 SQLite 消费/拒绝/无关会话测试后，相关 33 项再次通过。包含新旧 MCP 协议的新建→真实回执模拟→同聊天追问往返、仅一条批准审计、六个方向的能力检查、拒绝替代既有聊天、幂等重试与模式冲突、撤销/到期、错误原生回执、重启、同文不同轮、澄清转发、部署写入与启动失败回滚。原生应用仍由模拟回执替代，不能据此称真人创建验收通过。

已准备 deployment/enable-native-conversations.command（默认 dry-run，--apply 部署）。实际配置 dry-run 通过，只拟修改 server.json、专用入口项目 hooks.json、该项目 config.toml；保留旧 G 实验数据库和其他配置。部署需等待所有未完成任务结束，停服务释放锁后备份写入，重启失败尝试回滚并恢复服务。安装后用户需审核/信任更新的入口项目钩子、完全重开 Codex，并在所有发起应用重新加载 MCP。尚未执行部署。

剩余边界：其他两个宿主当前只有既有聊天能力；Codex 原生创建+回执+续接的组合仍待用户发起真人验收。PermissionRequest 已消费到原生执行之间不能原子撤销已发出动作。权限保证以本机可信 broker/钩子和普通模型工具边界为范围，不能抵御恶意同 UID 进程修改状态库。已有冷恢复和原生忙闲竞态限制继续适用。禁止由 root 自行发送验收消息。

### 部署后只读核对（2026-09-30）

用户确认已执行安装。部署脚本 dry-run 返回 Already installed，三处配置符合预期；服务监听 PID 4609，实际 connector_status 返回 codex_desktop 的 conversation_modes 为 existing/new。原生会话与操作表均为零行，尚无此次真人验收记录。本聊天当前公开的 delegate 工具签名仍未包含 conversation_mode，Antigravity 的本地工具缓存也未检出该字段；服务端配置生效不代表各客户端工具定义已刷新。需完成宿主重开/连接器重载后由用户发起新聊天及原通道追问验收。本轮仅检查状态，未发送任务或打开目标聊天。

### Antigravity 隔离新建实验成功（2026-09-30）

用户明确授权隔离尝试。官方 Sidecars 文档 https://antigravity.google/docs/sidecars 明确支持 agentapi new-conversation，并要求配置 projectId；本机 agentapi --help 也确认该命令。此前“当前桥接未接入新建”成立，但官方原生能力确实存在。检索同时检查了官方 antigravity-cli issue #519 的 projectsStore 失败案例，因此实际核对了桌面项目元数据。

本次仅新增 `/path/to/antigravity-native-create-probe` 一次性实验，未修改生产连接器代码、已有 Sidecar 或端点绑定。临时 Sidecar 使用已有测试项目，restart_policy=never；持久 intent 在执行前落盘，超时及重启均不重试创建。隔离模拟覆盖成功、超时、过期和重复启动。用户授予必要配置写入权限后安装，宿主自动加载并执行。

实际 agentapi 返回 conversationId=00000000-0000-0000-0000-000000000029，标题 Connector isolated creation test。随后复用现有连接器 SocketAdapter/confirm 和 Antigravity bridge 的 get-conversation-metadata，确认新 ID 属于项目 00000000-0000-0000-0000-000000000030 及原测试 workspace。宿主 transcript 的 MODEL/PLANNER_RESPONSE 实际返回 ANTIGRAVITY-NATIVE-CREATE-OK。实验原始回执、元数据校验与回复证据保留在实验目录。

临时 Sidecar 已移除，全局 config.json 恢复原始字节，临时配置备份已删除；保留新建测试聊天供用户查看。本结果证明官方接口和现有桥接身份核验可用于新建接入，不等于正式 connector_delegate(new) 已集成或一次批准的完整通路已验收；该生产能力仍未注册。

### 用户提出的对话清理约束（2026-09-30，待实现）

用户要求连接器具备清理其创建对话的能力，但必须由用户在发起端明确要求归档或删除对应对话，随后清除其连接器注册，避免活动条目堆积。这不是对当前测试聊天的删除或归档指令。

拟定边界：归档与删除为不同明确动作，不随 finish、空闲到期或任务完成自动执行；仅允许操作有真实创建回执、归属原发起端的连接器创建聊天。精确目标由持久绑定解析，不接受任意聊天 ID 绕过归属检查。宿主成功回执之后移除该聊天活动路由/注册和待派发项，保留最少的操作去重记录；共享端点配置及预先绑定的用户已有聊天不作为清理对象。失败或结果不明时保留可核查状态，不盲目重试；清理后的旧任务不可通过 continue 重新唤醒或创建聊天。具体接口、批准与状态设计尚未实施。

接口核查：官方 Sidecars 文档 https://antigravity.google/docs/sidecars 和本机 agentapi --help 目前仅列新建、发送（本机另有元数据查询），未列归档/删除。界面删除能力不能等同于可供连接器调用的清理接口，仍需独立验证。本轮只读检查与记录约束，未执行任何对话清理或修改生产实现。

### Antigravity 隔离归档与登记清理成功（2026-09-30）

用户要求隔离测试清理能力。本轮采用可恢复的归档动作，目标仅为上轮创建并有原生回执的测试聊天 00000000-0000-0000-0000-000000000029（Connector isolated creation test）；未测试删除。

本机 language_server 的 protobuf 描述符提供 UpdateConversationAnnotationsRequest：cascadeIds、annotations、mergeAnnotations；ConversationAnnotations 提供 archived 字段。宿主自带的内部调用示例使用 Sidecar 环境中注入的 ANTIGRAVITY_LS_ADDRESS / ANTIGRAVITY_CSRF_TOKEN，访问 loopback /exa.language_server_pb.LanguageServerService/<method>。探针仅从自身宿主环境取得连接信息，未复制或记录凭据。该 RPC 是安装版本内部契约，不是公开 agentapi 清理命令，不能承诺跨版本兼容。官方变更日志确认应用具有归档功能：https://www.antigravity.google/changelog?tab=engine 。未采用第三方数据库直接删除/改写方案。

`/path/to/antigravity-native-cleanup-probe/probe.py` 首先验证原生创建回执对应的独立登记目标及宿主项目/工作区，读取归档前状态，然后持久化一次性 intent；仅调用一次 UpdateConversationAnnotations({cascadeIds:[目标ID],annotations:{archived:true},mergeAnnotations:true})。宿主读回 archived=true 后才移除隔离 active-registration.json，保存 completed.json 作结果及防重记录。4 项模拟测试覆盖成功、宿主未确认、超时结果未知、目标错配，以及重放不重复归档、失败保留登记。

真实调用在 2026-09-30 09:35:27 America/Los_Angeles 完成。GetAllCascadeTrajectories 的目标记录从未设置 archived 变为 archived=true；只改变 archived 和 archivalStatusTimestamp，其他字段保持相同。隔离活动登记已移除。生产连接器数据库、共享端点与既有聊天绑定均未修改；这仅证明真实宿主归档和隔离登记清理的顺序，不代表正式 connector MCP 清理授权流程已集成。

临时 Sidecar 定义及配置项已移除，宿主 config.json 恢复原始字节，配置备份删除。本轮及上轮遗留的两个空 Sidecar 目录均成功清除。保留脚本、必要测试、请求、归档前后回执和完成证据；测试聊天留在宿主归档中。

### Antigravity 原生生命周期接入正式代码（2026-09-30，待部署验收）

用户要求将隔离测试通过的能力接入正式流程。本轮完成 `antigravity_sidecar` 提供者、新建后的原通道续接，以及独立 `connector_archive` 工具；未启用删除。没有发起新的真实模型任务或修改正在使用的宿主配置。

原缺口在适配层与生命周期边界：固定目标唤醒没有原生创建步骤，也没有“单独批准归档→确认宿主状态→解除活动绑定”的操作。现在 `native_host.py` 通过独立 Sidecar 执行已批准的创建和精确 ID 发送，复用原有消息、通道、审批、有效期和去重实现。新聊天只按显式 channel 领取任务；固定入口的无通道收件及旧唤醒调度器排除这些任务，避免误投旧对话。创建通过官方 agentapi，元数据核对项目/工作区；原生 ID 由宿主回执写入。消息已领取的证据不会被随后迟到的超时覆盖。

`connector_archive(channel, conversation_id, request_key)` 只向原任务发起端开放，目标必须对应本连接器已确认创建的聊天。重用既有 elicitation/强制宿主工具许可机制生成独立批准记录；原任务通信批准不能代替归档批准。归档获批后暂停原聊天消息，等待宿主空闲；撤销、过期、目标或配置漂移在外部操作前复核。只有 RPC 读回 archived=true 才删除 native_conversations 活动条目、原生操作与收件登记，并将源通道标为 archived。native_retired 留下最少的防重记录；旧编号、旧 continue 和新 key 均不能重新唤醒或创建替代聊天。普通 finish、到期和拒绝不触发归档。共享端点及用户预先绑定的聊天不属于清理对象。

归档沿用已验证的内部 UpdateConversationAnnotations RPC；独立 Sidecar 从宿主注入环境取得本机连接信息，未把凭据交给消息或模型。创建/发送/归档存在持久执行 intent；超时、断开和重启后的未知结果不自动重试。已确认未执行的归档失败恢复活动登记；结果未知保留暂停状态以待核查。没有增加“修改宿主数据库”或 UI 替代路径。

验证：完整回归 **612 passed，70.41 秒**。随后修正迟到超时的诊断记录，并补上“领取已发生、ACK 超时”及真实 Unix socket 的原生操作开关测试，相关 **23 passed，0.53 秒**。真实 stdio MCP 的 legacy/modern 协议覆盖独立归档批准、拒绝、取消、重放和归档后续接阻断；另外覆盖新建→新聊天领取→同 ID 追问，配置漂移与不支持目标、宿主忙碌、批准过期/撤销、目标错配、未知创建不重建、失败保留登记、成功去注册、部署就绪失败回滚。宿主调用在这些程序测试中模拟；不能据此宣称正式通道真人验收已通过。

部署入口：`deployment/enable-antigravity-native-conversations.command`，默认只读预览，`--apply` 执行。实际预检通过，拟改五处：server.json 的提供者登记、独立 Sidecar 定义、Antigravity 全局 Sidecar 启用项、已选项目的 connector_archive 精确工具入口许可、Codex 全局 enabled_tools/归档入口许可。独立归档任务仍须表单或强制宿主批准，不扩大文件/命令权限。复用部署生命周期代码，新增文件可回滚；服务重启并只读确认新桥接项目/工作区，失败恢复原配置与服务。现有 wakeup.json 固定目标及其他端点不改动。部署需用户终端执行，完成后重开 Codex、重载其他发起端 MCP，再由用户发起正式新建→续接→明确归档验收。

已知边界：Antigravity generic 凭据按端点认证，显式 channel 限制是路由隔离，不抵御持同一凭据的恶意客户端；同 UID 本机进程也不是隔离边界。内部归档 RPC 依赖当前安装版本。外部原生操作与撤销不能形成跨进程原子事务。未知结果目前保留记录人工核查，没有自动恢复命令。Codex/Claude 的归档与所有宿主的删除均未列入已支持能力。


### Antigravity 原生生命周期真人验收通过（2026-09-30，新聊天接续）

用户在发起端分三轮明确下达新建、续接和归档指令，并确认“已经通过验收”。本聊天加载的新 MCP 签名包含 conversation_mode 和 connector_archive；实际 connector_status 声明 gemini 支持 existing/new 及 archive。

首轮 connector_delegate(conversation_mode=new) 返回真实创建回执 conversation.created=true，原生 ID 00000000-0000-0000-0000-000000000031，通道 00000000-0000-0000-0000-000000000032；Antigravity 实际回答 47 × 19 = 893。第二轮 connector_continue 沿同一通道及同一原生 ID 返回 893 + 7 = 900，没有另建 delegate 通道。第三轮 connector_archive 单独批准，归档请求通道 00000000-0000-0000-0000-000000000033，返回 state=archived、registration_removed=true。

随后仅只读核对生产数据库：归档记录 confirmed，原生宿主 receipt.archived=true；源通道 archived，native_conversations 和 native_operations 的该通道活动行均为 0；native_retired 保存对应真实 ID 和归档通道。批准审计覆盖任务与独立归档，详细证据见 deployment/antigravity-native-live-20260930.json。未额外发送归档后的 continue 探针；该拒绝行为由已有回归覆盖，本轮真人未另测。

用户下一步要求同样完善 Codex → Claude Code Desktop 原生新建、原聊天续接、单独审批归档，尽可能复用公共通道代码，先由 Astra 做详细诊断和解决方案再实施。此时 Claude 仍只声明 existing，不将历史既有聊天往返当作原生新建验收。


### Codex → Claude Desktop 原生生命周期：Astra 诊断与只读预检（2026-09-30，新聊天接续）

按用户要求，先调用 gpt-6-astra 对现有连接器、Claude 安装包和官方/候选实现作详细只读诊断，再实施证据允许的部分。Claude 具备 start_session、get_session、send_message、archive_session，但原生创建检查当前轮次为人类本地请求；连接器自动 inbox 不能用 user_asked 伪装来源，普通 PreToolUse allow 不能替代该门禁。归档具有宿主自身 consent 与可见/运行/子会话保护，官方说明需要 Claude 原生批准。已向用户提出是否接受 Claude 侧必要确认，尚待选择；未启用生产 new/archive provider。

检查期间安装包从 2.9939.4 变成 2.16120.0，非本次工作触发。Astra 和主代理重新核对新版本，原创建来源门禁仍在。当前主要实现 index.chunk-Des3qCSt.js、index.chunk-Nnw-A1di.js；不能沿用旧分块宣布接口有效。Desktop identity local_00000000-0000-0000-0000-000000000002 与 CLI UUID 00000000-0000-0000-0000-000000000002 是两层身份，须用宿主元数据核对映射，不能套 Antigravity 纯 UUID 规则。

新增 integrations/claude_code/native_preflight.py：标准库只读检查版本、ASAR 边界与已核查源码 SHA256，明确指定入口的元数据只输出身份/cwd/归档白名单；安装变化快速失败，未核查版本或hash漂移为 unverified。静态识别不提升运行时能力；runtime_tools/authorization_transfer 仍 unverified，native_provider_ready=false，idle=unknown。实际预检已核对 2.16120.0、48,514,013 字节安装包及入口身份/工作目录/未归档状态。

专用测试 20 passed，0.03 秒，覆盖损坏/越界 ASAR、unpacked/link、身份与类型错配、缺 CLI 映射、未知版本、hash漂移、检查期间版本更新、白名单及静态识别不启用provider。本轮未重跑全套，因为生产通道/桥接代码未改；未创建聊天、发送模型任务、部署或归档。已有 Claude bridge 的 status/reconcile unknown 与写 socket ambiguous 契约保持原样。

后续优先 claude_ingress 共用精确计划/真实 PostToolUse 回执与公共审批、续接和归档状态机；先由用户在 Claude 入口验证实际工具可见性和get_session回执。最明确创建路径需用户在Claude本地亲自请求start_session(user_asked)。own_initiative仅返回task_id；卡片点击后的创建不会补发原PostToolUse回执，须另外验证spawnedFrom父身份+taskId唯一关联，不可当成已取得create receipt。offer也要求person来源。归档读回与可能alsoArchived级联须单独验证。

详细用户报告保存在维护者本机，未随源码发布。官方证据：https://code.claude.com/docs/en/desktop#work-across-sessions 、https://code.claude.com/docs/en/hooks 、https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link 。


### Claude 接入补查：保留 Codex 单端审批目标（2026-09-30，新聊天接续）

用户表示不太能接受新建和归档增加 Claude 侧确认，要求继续找其他途径，或提供提示词让 Claude 自检。暂不采用上一轮带目标端确认的方案。补查官方 Channels / Channels reference：支持已注册、用户选定的channel转发工具permission_request并接回匹配ID的批准；公开说明主要覆盖运行中的Claude Code会话，未证明能替代Desktop原生创建的人类来源检查或archive的专属consent。Dispatch有用户远程请求到Code的路径，但未找到供本地连接器复用的公开入口。

当前2.16120.0安装源码再核对：assertCanStartSideSession仍要求本地person来源；noteCliInput区分machineSent/seeded_summon/peer origin；安装包含channelsEnabled/allowedChannelPlugins配置schema，不代表Desktop运行时已接入Channels。归档宿主有withArchiveConsent与hook/工具许可处理的关系，及内部模式分支，应由Claude自检具体支持路径；不能仅凭官方默认流程断言所有宿主授权路线都不可能。未调整权限模式、修改配置或发出模型任务。给用户可复制的只读自检提示词，要求实际工具/schema、宿主受支持的授权传递/permission relay、真实ID与回执、公共代码复用点及可证伪的验证步骤。官方来源：https://code.claude.com/docs/en/channels-reference#relay-permission-prompts 。

### Desktop-native Claude session inspection (2026-10-03)

Following Astra ultra design and the owner’s Desktop-only scope correction, two opt-in read-only MCP tools now list Claude Code chats and inspect the exact user-selected row. Shared identity, metadata and socket validators were moved into the installed package and reused by existing bridge/onboarding callers. Selection is not enrollment consent: unbound targets stop before private-record access. Existing-target inspection rechecks metadata, binding and full server/wakeup snapshots before reporting local verification. Standalone bridge/preflight imports remain compatible with system Python.

Validation: full regression 736 passed before the last focused test additions; the final focused MCP suite passed 18 tests, including configuration drift during validation, secret-free results, no broker invocation and unchanged files. A separate packaged runtime imports without source PYTHONPATH. Exact configured launches list 10 tools for gpt and 13 for claude_code; installed module hash matches final source. Only Codex and Claude project MCP launch stanzas changed, with byte backups retained; endpoint credentials and worker bindings were unchanged, and the broker was not restarted.

Native-host acceptance remains pending. The current Claude MCP panel reports Connected with its cached 7 tools and exposes no observed refresh control. Codex computer use is explicitly prohibited by the tool, so normal Codex reconnect requires the user. No native enrollment receipt retry, new binding, message dispatch or archive occurred in this phase. Evidence is in the task’s work/native-binding-activation.json and work/native-binding-configured-verification.json.


### Codex cold-owner timeout recovery and Antigravity permission audit (2026-10-04)

An approved selected-chat task stalled because its registered ingress had no responsive owner and the five-second Desktop probe returned generic unavailable. The installed Codex 26.928.31416 router allows ten seconds for client discovery before returning no-client-found; its ordinary discovery only finds an already loaded owner. Manual navigation to the exact registered ingress enabled the pending task to finish once, confirming the readiness boundary. The installed contract also exposed an unrecognized native absence response: no-client-found.

The probe now distinguishes an owner-discovery receive timeout after the request was sent from connection, initialization, write and snapshot failures. Only owner_discovery_timeout and the existing explicit absence/startup reasons enter the already opted-in one-shot navigation recovery. Timeout is not treated as proof of owner absence. Recovery still requires the frozen target and live task authority, records navigation intent durably, and repeats fresh identity and idle checks before sending. A send-phase error is retryable only before the send side effect starts; an uncertain send remains ambiguous. Native no-client-found now maps to no_owner only for discovery.

Focused regression passed 136 tests across status probe, bridge and wakeup; root then passed the full 878-test suite in 87.35 seconds. Five final partial-header/payload and native-absence cases were added afterward; the affected probe/status suites passed 31 tests. Coverage includes phase-specific timeout classification, native absence, exact target, authority loss, restore opt-out, one-shot navigation and preserved ambiguous-send behavior. Deployment and autonomous cold-start acceptance are separate from these isolated tests.

Antigravity 2.19.1's persisted native protobuf schemas confirm that both execution configs for the observed chat contain all eleven exact connector MCP tools in effective_grants.allow. The only effective Ask is an unrelated chrome_devtools tool, and actual server/tool names match the configured grants. Stored interactions contain one binding elicitation, one task elicitation and seven file-read permission requests. Repeated quick_bind/receive steps contain no stored MCP permission interaction; their allow metadata is a pre-tool hook decision, not a user-permission receipt. These records do not establish the cause or absence of the human-reported repeated MCP popup. No host permission policy was changed; the next genuine pending popup's action/resource and matching host decision are needed for a justified policy fix.

Evidence sources: installed Codex bootstrap-B7ariqxX.js SHA256 ad9f3da3d96e6713c89b800d1e0c369f8fad1cc20af8233cf7bd906550a2a5fd; Antigravity language_server SHA256 d36df3e4638094a4be2e968868228b019460cd608212ae8bcd6ad8394faf65f2; https://learn.chatgpt.com/docs/reference/commands ; https://github.com/openai/codex/issues/37397 ; https://antigravity.google/docs/permissions . Detailed scope and evidence are in a maintainer-local report (not published).

### Open-source release preparation (2026-10-05)

Public/private boundary: dated verification records, probe traces and generated host configs under `deployment/` and four evidence JSON files under `docs/` stay on the maintainer's machine and are excluded by `.gitignore`. Retired one-off experiments pinned to the maintainer's own chats are excluded as well: the Codex shared-server and creation-hook probes and `integrations/codex_desktop/live_probe.py` with its test. Nothing was moved or deleted. The installed data directory, host configurations and the live service were not touched.

The three owner scripts that hard-coded the maintainer's Codex thread, workspace and Antigravity project now require explicit `--thread-id`/`--workspace` or `--worker-project`/`--antigravity-project-id` arguments, and argument parsing happens before any file access. Test fixtures use synthetic IDs. Placeholder configurations for `wakeup.json`, `server.json` conversations, the stdio launcher and the Antigravity sidecar live in `examples/configs/` and are loaded by the real parsers in `tests/test_example_configs.py`. No `src/` or bridge code changed.

Local chat, session, project, channel and message IDs quoted in the dated sections of this file, `GENERALIZATION.md` and `ZCODE_WAKEUP_RESEARCH.md` were replaced with numbered placeholders (`00000000-0000-0000-0000-0000000000NN`). The same original ID always maps to the same placeholder, so cross-references within and across these files remain consistent. Owner-specific folders became `/path/to/...` and the numeric launchd user domain became `gui/<uid>`. The maintainer-local evidence keeps the original values.

Documentation is English-first with Chinese as the secondary language: `README.md` (English) is primary and `README.zh-CN.md` keeps the full Chinese guide. `CONTRIBUTING.md` lists the safety rules contributors must not break; `SECURITY.md` asks for private vulnerability reports; `docs/ARCHITECTURE.md` is the English code map. `.github/workflows/tests.yml` runs the locked test suite on a macOS runner (not yet observed on GitHub).
