# ZCode 原任务自动唤醒核验

日期：2026-09-16（本机时区）。本机版本：ZCode Desktop 3.11.2，Agent 运行时 0.16.5。

## 结论

已经找到能够向既有桌面任务提交指令的技术路径：ZCode Web Remote Control。官方文档、已安装应用的实现及一个社区适配器的实际代码相互印证。

当前结论属于**接口与实现核验**，尚未完成本机远程配对和真实唤醒测试，不能报告“自动唤醒已经实现”。现有连接器仍要求接收方主动调用 `connector_receive`。

初次静态核验未使用 Computer Use，未启用远程控制，未读取配对凭据，未修改 ZCode 安装文件或连接器业务代码。后续实际尝试见下节。

## 用户批准后的首次实际尝试

### 后续重试结果

用户确认“按你建议的来”后，再次核实锁的 PID 仍属于 Siri，将整个旧锁目录移动到 `work/zcode-wakeup-test/setting.json.lock.backup`。未修改设置文件内容；恢复记录保存在同目录 `lock-recovery.json`。随后 ZCode 远程控制界面显示“已就绪”，配置锁阻塞已解除。

用户随后提示 GLM 五小时配额可能耗尽，因此本轮改为只读检查，没有发送模型指令。第一次接口配对等待 12 秒超时；所使用的磁盘凭据是封装格式，不能把此结果当作正式链接协议不可用的证据。改用界面正式复制链接时，浏览器工具的虚拟剪贴板无法粘贴原生应用的系统剪贴板内容，本机临时表单未提交。打开过文本编辑的文件选择窗口，但没有将配对链接粘贴到文稿。

收尾已通过界面确认刷新二维码，使此前复制的链接失效；随后点击停止，ZCode 显示“已关闭 Web 远程控制”。设置中的自动恢复标记已不存在，原锁位置也已无遗留锁。保留旧锁备份；清理本次临时网页与脚本。

本轮最终结论：**远程控制启动已恢复；正式配对、读取原任务、自动唤醒与回复仍未通过。未提交模型请求。**证据见 本次尝试记录（`docs/zcode-wakeup-attempt.json`，维护者本机记录，未随源码发布）。额度恢复后，下一次验证还需解决正式配对链接的安全交接，再执行单条合成指令，不能直接跳到连接器集成。

### 首次失败与根因记录

用户回复“尝试一下”后，通过 Computer Use 打开原有测试任务的远程控制入口。ZCode 报告连接失败：等待 `~/.zcode/v2/setting.json.lock` 超过 8003 毫秒；复制链接按钮不可用，未生成可用配对链接。未发送合成指令，也未建立远程客户端连接。

只读排查确认：

- 锁目录约 6 天前创建，唯一 owner 元数据记录 PID 1098。
- 当前 PID 1098 的程序为 macOS `siriinferenced`；当前 ZCode 主进程为 PID 27084，已运行约 3 天 18 小时。
- 本机包的 `isProcessAlive` 只用 `process.kill(pid, 0)` 判断存活；`isOwnerFileReclaimable` 对有 PID 的锁只检查该 PID 是否死亡。没有核对进程身份或启动时间。因此旧编号被其他存活进程占用时，自动回收逻辑不会清除该锁。

已向用户请求将旧锁目录备份到项目后重试。该锁早于本次作业，按用户“只清理自己产生的问题”的约定，没有擅自移动或删除。点击停止后配对面板已消失，原任务页面仍显示此前的求和结果。

本次状态：**受既有配置锁阻挡，真实自动唤醒仍未验证**。Computer Use 仅用于开启、观察和停止远程控制；未通过界面提交测试消息。

## 为什么现在需要先提交接收指令

`mcp_server.py` 的 `connector_receive` 将调用交给 `Broker.receive`；后者等待 `asyncio.Condition`。`Broker.send` 与批准求助的逻辑写入数据库后通知已经存在的等待者。

这一通知只能唤醒连接器程序内的等待调用。闲置的桌面 AI 尚未调用工具时，没有正在等待的调用可以把消息带回模型，也没有代码向 ZCode 宿主请求开始新一轮处理。缺口位于桌面任务启动入口。

## 核验结果

| 路线 | 已核实的事实 | 对本目标的判断 |
| --- | --- | --- |
| 当前 MCP 收信 | 代码实现为客户端调用后等待；未发现本项目向宿主提交新回合的逻辑 | 只能覆盖已开始接收的任务 |
| 独立 `app-server` / ACP / MCP bridge | 社区方案通过自己启动的标准输入输出子进程调用会话接口 | 恢复相同历史编号不等于控制桌面正在使用的运行进程，不能据此宣称通过 |
| ZCode 深层链接 | 本机主进程路由检查到工作区打开、支付及 OAuth 回调 | 本轮未找到带任务编号和指令内容的提交入口 |
| Web Remote Control | 官方明确可继续已有桌面会话；本机实现会附接当前窗口的 host process | 符合保持原桌面运行进程的要求；需要配对后实测 |
| Bot Channel | 官方支持通过微信、飞书驱动已有桌面会话 | 具备产品能力，但额外账号、平台接入和消息路径不适合本轮最小验证 |

MCP 运行时包中出现 `sampling/createMessage` 等字符串，多处属于 SDK 协议定义；这些字符串不构成 ZCode 已注册自动唤醒处理器的证据。本轮未证明标准 MCP 通知能够让闲置任务开始处理消息。

## 本机实现证据

只读检查 `/Applications/ZCode.app/Contents/Resources/app.asar`：

- `out/main/index.js` 的 `createWebRemoteControlManager` 接收 `workspace-bridge-open`，调用 `attachWorkspaceHost`，将远程 RPC 与宿主连接相互转发。
- `createWebRemoteControlSharedHostAttachments` 中的 `attachLocalHost` 从 `windowHostProcessMap` 找到已有窗口进程，通过 Electron MessagePort 和 `AttachServicePort` 附接；这一分支没有另起 Agent 进程。
- 远程传输使用外部 WebSocket relay，包含注册、身份挑战、配对和连接状态处理。它不属于本项目原有的纯本机 HTTP 通道。
- `out/host/index.js` 保留 `sendPrompt` 和 `getTaskSnapshot` 等任务方法；`sendPrompt` 按 `taskId` 解析已有任务，再交给 Agent 服务提交内容。
- 同文件处理 `AttachServicePort`，按作用域附接服务端口，与主进程调用链对应。

核验时 `out/host/index.js` 的 SHA-256：

`30911a90dadc5c384959d00d95ccc70c8cf38c74a9cb99c3168b0897d046d215`

以上是静态调用链证据；不能代替真实认证、协议兼容、模型开始运行和结果返回的验证。

## 现有方案与取舍

### 优先验证：复用远程控制协议的方法

[codex-zcode-remote-relay](https://github.com/Zhi-Chao-PAN/codex-zcode-remote-relay) 的实际客户端代码包含：

1. 从用户提供的远程控制链接获取配对参数。
2. 连接 ZCode 的 WebSocket 中继并完成 HMAC 挑战认证。
3. 按工作区与任务编号打开桥接。
4. 调用 `zcode-task.getTaskSnapshot` 和 `zcode-task.sendPrompt`。

其 [`dispatch.mjs`](https://github.com/Zhi-Chao-PAN/codex-zcode-remote-relay/blob/main/scripts/src/dispatch.mjs) 有复用当前任务的分支，证明方案并非仅支持新建任务；本项目验证应显式固定测试任务编号，不能沿用默认选择第一个任务的逻辑。

适配器 [`zcode-remote-client.mjs`](https://github.com/Zhi-Chao-PAN/codex-zcode-remote-relay/blob/main/scripts/src/zcode-remote-client.mjs) 默认实际连接 `wss://zcode.z.ai/ws`。即使仓库描述使用“local”，消息仍经外部中继。

维护与适配情况：仓库页面显示 MIT 许可证、6 次提交、0 个公开问题，未显示发布版本；脚本包版本为 0.2.0，要求 Node 24 及以上。仓库自称个人实验，协议未正式公开。`prepare.mjs` 使用 PowerShell 启动辅助脚本，不能原样作为 macOS 启动器。项目声称的 138 项测试不是本项目实测结果。提交详情与部分网页读取失败，不能给出经过核实的最近维护日期。源码克隆分别遇到 DNS 和 TLS 连接失败；关键客户端与调用方通过网页源码完成检查，未安装或执行该仓库代码。

选择：借鉴最小认证、附接及发送链路，先做一次有界验证；不直接引入整个委派、模型选择、工作池和后台监控框架。尚未复制第三方代码。

### 其他路线

- [zcode-acp](https://github.com/william0wang/zcode-acp/blob/main/docs/PROTOCOL.md) 与 [JetBrains app-server 协议说明](https://github.com/csuftt/zcode-jetbrains-plugin/blob/master/docs/zcode-appserver-protocol.md)：适合自有运行进程的集成，本轮不采用。
- [coder-mcp-bridge](https://github.com/Deslord319/coder-mcp-bridge)：提供独立后端调度能力，不能仅凭其会话恢复能力证明桌面原任务唤醒。
- GUI 输入可触发任务，但仍有焦点与审批界面依赖，不作为此次自动接口能力的成功证据。

## 最小实测与成功标准

待用户确认是否接受临时外部中继配对。原因是配对链接提供桌面窗口控制权，范围大于此前批准的仅本机合成端点测试；此前授权不能直接推定包括此变化。

确认后只测试既有 ZCode 任务：

- 工作区：`work/desktop-test/zcode-test`。
- 任务：`sess_00000000-0000-0000-0000-000000000051`。
- 指令：只回复本轮生成的唯一测试标记，不编辑文件、不调用外部业务工具。

步骤与判据：

1. 确认测试任务空闲，记录同一任务的快照。
2. 临时开启远程控制，使用正式配对链接认证；不记录或展示链接内容。
3. 显式指定工作区和任务编号，通过接口提交一次合成指令。
4. 读取同一任务的新回合和准确回复，记录提交、开始、完成时间；仅接口返回成功不足以通过。
5. 关闭本轮连接，停止远程控制并使测试配对链接失效；保留不含凭据的证据。

通过以上步骤后，才能开发“批准并保存消息 → 唤醒目标任务 → 原生 MCP 领取与回复”的连接器整合。整合还需验证任务忙碌、重复通知、断连、授权到期与撤销，防止重复执行或在授权结束后启动新回合。

## 官方资料

- [Remote Control](https://zcode.z.ai/en/docs/remote-control)：控制已有桌面会话；配对链接授权及停止方式。
- [Bot Channel](https://zcode.z.ai/en/docs/bot-channel)：微信／飞书入口驱动已有桌面会话。
- [MCP Services](https://zcode.z.ai/en/docs/mcp-services)：工具接入与配置，未从该文档找到闲置任务自动启动接口。
