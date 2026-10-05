# MCP 兼容性核查

核查日期：2026-09-19。

## 结论与范围

通用 MCP 是当前连接器合理的接入方向。本轮选取的 14 个开发类 agent／harness 产品或入口中，13 个官方文档提供内置 MCP 接入，Pi 核心明确不内置，需扩展。这个样本说明覆盖面广，不是市场占有率统计，也不能证明所有 agent 均支持。

最初核查阶段完成官方文档及一方代码仓库核查，并复核项目接入代码；没有逐个安装这些产品。随后已完成通用化与程序传输测试，结果见 [验证记录](VALIDATION.md)；仍未新增跨产品宿主运行验收。下表的“支持”表示文档支持，不表示当前连接器已经在该产品上通过测试。产品版本、运行入口、组织策略和配置仍影响实际可用性。

## 产品兼容表

stdio 指宿主启动本机 MCP 子进程；Streamable HTTP 指标准 MCP HTTP 传输。表中的“远程 HTTP”只沿用所核查文档的表述，不据此断言其支持全部协议版本。

| 产品／入口 | 内置 MCP 接入 | 已核查传输 | 官方依据及限制 |
| --- | --- | --- | --- |
| Codex 本地 CLI／IDE／桌面入口 | 是 | stdio、Streamable HTTP | [MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。本地配置与 Web 入口不同。 |
| Claude Code | 是 | stdio、Streamable HTTP、SSE | [MCP 文档](https://code.claude.com/docs/en/mcp)。推送另有 Channels 扩展。 |
| Cursor | 是 | stdio、Streamable HTTP、SSE | [MCP 文档](https://cursor.com/docs/mcp)。需配置服务器与权限。 |
| VS Code／GitHub Copilot | 是 | 本地 stdio、远程 HTTP | [服务器配置](https://code.visualstudio.com/docs/agent-customization/mcp-servers)。Agent Host 的配置加载路径有差异。 |
| GitHub Copilot CLI | 是 | stdio、Streamable HTTP、SSE | [添加服务器](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)。组织允许列表可能限制服务器。 |
| Windsurf／Cascade | 是 | stdio、Streamable HTTP、SSE | [官方 MCP 页面](https://docs.devin.ai/desktop/cascade/mcp)。原 Windsurf 文档地址当前重定向至 Devin Desktop。 |
| Cline | 是 | stdio、Streamable HTTP、SSE | [MCP 概览](https://docs.cline.bot/mcp/mcp-overview)。远程连接推荐 Streamable HTTP。 |
| Roo Code | 是 | stdio、Streamable HTTP、SSE | [MCP 使用说明](https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo/)。工具超时可配置。 |
| Gemini CLI | 是 | stdio、Streamable HTTP、SSE | [MCP 服务器](https://geminicli.com/docs/tools/mcp-server/)。工具 schema 需要符合宿主要求。 |
| OpenCode | 是 | 本地进程、远程 HTTP | [MCP 服务器](https://opencode.ai/docs/mcp-servers/)。local／remote 配置分开。 |
| Kiro IDE／CLI／Web | 是 | stdio、远程 HTTP／SSE | [MCP 支持表](https://kiro.dev/docs/mcp/)。同页明确 Mobile 当前不支持。 |
| ZCode | 是 | stdio、HTTP、SSE | [MCP Services](https://zcode.z.ai/en/docs/mcp-services)。支持导入部分其他客户端配置。 |
| goose | 是 | stdio、Streamable HTTP | [Using Extensions](https://goose-docs.ai/docs/getting-started/using-extensions/)。扩展接入基于 MCP，可添加自定义服务器。 |
| Pi | 否；可加扩展 | 由扩展决定 | [核心 README](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/README.md#philosophy) 明确写明 “No MCP”，建议通过扩展增加支持。 |

框架层也已有接入能力：[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/mcp/) 提供 stdio、SSE、Streamable HTTP 等接口；[LangChain](https://docs.langchain.com/oss/python/langchain/mcp) 当前文档提供 `langchain.mcp.MCPAdapter`，并标注其为 beta。框架能加载 MCP 工具，不等于已经建立常驻接收消息的 agent 运行循环。

## 工具调用与消息触发

要分别验证两件事：agent 能否主动调用连接器工具，以及外部消息能否触发指定会话开始处理。上面的兼容表只直接证明前者。

### Claude Code：已提供基于 MCP 的推送扩展

[Channels](https://code.claude.com/docs/en/channels) 支持向已运行、保持打开的本地会话推送事件，并让 Claude 开始处理。当前仍为 research preview，受组织启用与插件允许列表等条件限制，不能假定每个安装环境都已开放。

[Channels reference](https://code.claude.com/docs/en/channels-reference) 明确使用 stdio MCP、`experimental['claude/channel']` 能力以及 `notifications/claude/channel` 通知。它是宿主专有扩展，不能由普通 MCP 工具支持推导出来。通知写入成功也不代表模型处理成功，若需要可靠交付，仍要保留应用层回执与消息状态。

因此，“MCP 一定只能被动调用、无法触发处理”的说法过于绝对；准确结论是：已有宿主通过 MCP 扩展支持推送，但这一能力尚不能作为跨宿主共同前提。

### OpenCode：提供会话消息 API

[Server 文档](https://opencode.ai/docs/server/) 提供 `POST /session/:id/message` 和 `POST /session/:id/prompt_async`，可以向指定会话同步或异步提交消息。这是一条可适配的程序化触发路径，需要可访问的服务与有效会话，不能仅凭安装 MCP 推断已经可用。

其他产品本轮没有完成逐一触发接口核查，不能标记为“没有自动触发能力”。

## 改造前的代码评估

以下为本轮核查时、实施前的代码状态；2026-09-19 已完成通用化，当前行为以 [README](../README.zh-CN.md) 和 [验证记录](VALIDATION.md) 为准。原评估用于说明改动原因：

| 代码位置 | 当前行为 | 通用化需要处理的部分 |
| --- | --- | --- |
| `src/local_ai_connector/mcp_server.py` | 提供求助、接收、发送、结束、状态五个工具；仅通过 stdio 启动 | 复用现有工具，按需要增加标准 Streamable HTTP 入口 |
| `src/local_ai_connector/cli.py` | 初始化固定两个端点，并分别生成 Codex／ZCode 配置 | 支持任意命名端点及通用配置；宿主配置格式作为安装差异处理 |
| `src/local_ai_connector/identity.py` | Codex／ZCode 读取各自元数据；generic 使用进程随机 UUID，首次绑定写入配置 | 明确定义通用端点身份与会话身份；generic 重启后 UUID 改变会与已保存绑定冲突 |
| `src/local_ai_connector/server.py`、`client.py` | 内部通过自定义 HTTP `/call` 通信 | 此路由不是标准 MCP HTTP 端点，不能直接把它填入任意客户端的 MCP URL |
| `connector_receive` | 默认等待 300 秒 | 与各宿主工具超时匹配；可采用较短等待并复用游标，不能默认所有宿主允许长时间调用 |

身份尤其需要明确：同一 MCP 服务器可能被多个任务共用。产品支持 MCP 不代表它会提供可信的原生任务 ID。没有这类元数据时，需要由连接器配置或认证凭据界定端点隔离范围，不能声称已自动识别每个桌面任务。

## 实施计划与后续验收

1. 已完成通用 MCP 工具接入：保留 stdio，支持任意端点名称，解决 generic 身份重启与隔离契约。
2. 已增加标准 Streamable HTTP，共享本机 `/mcp` URL，继续复用现有消息存储、去重、游标和回执。
3. 用至少三种不同宿主验证发现工具、消息往返、超时续收、进程重启及多会话隔离。当前只有文档覆盖，尚未完成这一轮运行验证。
4. 自动唤醒按用户要求保持隔离，未接入运行包；Channels 与宿主会话 API 留待后续单独评估。

实施后的成功标准应是：同一套连接器工具可由不同 MCP 宿主配置使用，消息可恢复且不会串入其他端点；只有通过对应宿主实测后，才声明支持无人值守触发。


## 实现选型补充

复用项目锁定的官方 [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) 2.2.0，使用其 stdio 运行器和 `streamable_http_app()`，没有新增协议实现或第三方桥接依赖。[MCP 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) 提供 stdio／Streamable HTTP 的协议依据。

选用无状态 HTTP：应用消息与授权原本由 Broker／SQLite 保存，工具不依赖 MCP 传输会话。这样重连无需绑定旧 MCP session ID；恢复仍使用应用游标与去重编号。本机 HTTP 复用现有端点凭据，并在协议入口校验每个请求。
