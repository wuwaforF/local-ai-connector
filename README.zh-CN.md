# 本地 AI 连接器

[English](README.md) | **简体中文**

本地 AI 连接器通过你电脑上的一个本机服务，让不同桌面应用里的 AI 助手互相交接任务。例如，Claude 的聊天可以请
Antigravity 的聊天审阅一段代码：

- 每个任务都在发起它的应用中由你批准。
- 任务只送到你为这项工作绑定的聊天。
- 工作者的真实回答会回到提出请求的聊天。

支持的应用：Codex、Claude Desktop（Code 标签）和 Antigravity，覆盖 macOS、Windows 和 Linux，各自的支持程度见
[当前支持情况](#当前支持情况)。

> **公开预览版，尚不适合生产使用。** 目前需要让工作者聊天主动接收任务，尚未实现自动唤醒。到目前为止，真实桌面验证
> 只覆盖 macOS 上的 Antigravity。

## 工作方式

1. **每个应用安装一次。** `local-ai-connector setup <应用>` 把连接器加入该应用的 MCP 设置。它从不授予工具或文件
   权限，只告诉你需要允许哪些。
2. **绑定工作者聊天。** 在要接收任务的聊天里说 *"把这个聊天用于连接器任务"*，应用会请你批准。
   - 聊天由应用本身识别，模型无法选择。
   - 绑定本身不授予任何任务权限。
   - 以后要换聊天，绑定另一个即可，无需重新安装。
3. **发起任务。** 在另一个应用的聊天里请求帮助，例如 *"让 Antigravity 检查这个函数"*。
   - 你在那里批准任务。
   - 批准会把任务固定到当时绑定的聊天。
   - 之后重新绑定也不会改变已批准任务的去向。
4. **接收并回答。** 在工作者聊天里说 *"检查连接器任务"*，它会读取任务并回复。同一应用的其他聊天无法读取或回答
   这个任务。
5. **取得结果。** 回复会回到发起任务的聊天，后续轮次仍在同一聊天中进行。

## 安装

准备：

- [uv](https://docs.astral.sh/uv/getting-started/installation/)，它会提供 Python 3.13。
- 要连接的桌面应用。
- 使用 Claude 时，需要 Claude Code 的 `claude` 命令在 PATH 中。在 macOS 上，如果 PATH 中没有 `claude`，会使用 Claude
  Desktop 自带的版本。

```sh
git clone https://github.com/wuwaforF/local-ai-connector.git
cd local-ai-connector
uv sync --frozen
uv run local-ai-connector setup antigravity     # 或：codex、claude
uv run local-ai-connector doctor
```

运行 `setup` 之后：

- 按 `setup` 的提示，重启该应用或重新连接它的 MCP 服务器。
- 不要移动克隆目录：应用里的配置指向这个目录的 Python 环境，移动后需要重新运行 `setup`。
- `setup` 不会覆盖不是它创建的 MCP 配置项、数据目录或端口。要与已有安装并存，请使用 `--profile <名称>`。
- `uninstall` 只移除本安装添加的内容；`uninstall --purge` 还会删除它的数据。

## 当前支持情况

| | macOS | Windows | Linux |
| --- | --- | --- | --- |
| 服务、`setup`/`doctor`/`uninstall`、批准与绑定规则 | 自动化测试 | 自动化测试 | 自动化测试 |
| Antigravity 精确聊天绑定与隔离 | **已在真实桌面验证**（Antigravity 2.19.1） | 仅自动化测试 | 仅自动化测试 |
| Codex 与 Claude Desktop Code 的单聊天身份 | 仅自动化测试 | 仅自动化测试 | 仅自动化测试 |
| 从真实发起端桌面完成端到端批准 | 待验收 | 待验收 | 待验收 |
| 自动唤醒工作者聊天 | 未实现 | 未实现 | 未实现 |

各项的含义：

- **macOS 上的 Antigravity：** 精确聊天绑定与隔离已在 Antigravity 2.19.1 上验证，使用独立测试档案和两个真实聊天。
  - 已绑定的聊天收到并回答了自己的任务。
  - 另一个聊天无法读取或回答这个任务。
  - 重新绑定后，已批准的任务仍留在原来的聊天。
  - 重启 Antigravity 后以上结果依然成立。
- **Windows 和 Linux** 有 CI 中的自动化测试，但尚未在真实 Antigravity 上验收（[#3](https://github.com/wuwaforF/local-ai-connector/issues/3)）。
- **需要主动接收任务：** 目前要让工作者聊天主动接收任务，尚未实现自动唤醒（[#1](https://github.com/wuwaforF/local-ai-connector/issues/1)）。
- **从真实发起端桌面完成的端到端批准仍待验收**（[#2](https://github.com/wuwaforF/local-ai-connector/issues/2)）。在
  Antigravity 测试中，每个任务都由真人批准，但发起端是终端代替的。
- **Antigravity 的会话元数据键没有公开文档。** 这个键是 `antigravity.google/conversation_id`，在 2.19.1 上观察到。
  如果某个版本不再发送它，绑定会安全失败并返回 `missing_session_identity`，不会退回到无法验证的身份
  （[#4](https://github.com/wuwaforF/local-ai-connector/issues/4)）。
- **Codex 与 Claude** 的聊天身份尚未在真实桌面确认（[#5](https://github.com/wuwaforF/local-ai-connector/issues/5)）。
- **不隔离你自己的程序：** 以你的操作系统用户身份运行的其他程序可以读取连接器的本机数据。它防的是其他用户，不防同一
  用户的进程。

## 功能

**现已提供**

- 所有已连接应用共用一个本机服务，在 macOS、Windows 和 Linux 上按应用分别安装。
- 相互独立的安装档案。
- 用自然语言绑定工作者聊天，并在该聊天中批准；重新绑定无需重新安装。
- 每个任务都在发起应用中批准，批准会固定目标聊天及其绑定版本。
- 同一应用的不同聊天互相隔离，返回真实回答，支持追问以及同一任务的后续轮次。

**计划中**

- 自动唤醒已绑定的聊天（[#1](https://github.com/wuwaforF/local-ai-connector/issues/1)）。
- 在所有平台完成真实桌面验收（[#2](https://github.com/wuwaforF/local-ai-connector/issues/2)、[#3](https://github.com/wuwaforF/local-ai-connector/issues/3)、[#5](https://github.com/wuwaforF/local-ai-connector/issues/5)）。
- 登录时自动启动（[#6](https://github.com/wuwaforF/local-ai-connector/issues/6)）。
- 从发布包安装（[#9](https://github.com/wuwaforF/local-ai-connector/issues/9)）。
- 英文命令行提示（[#8](https://github.com/wuwaforF/local-ai-connector/issues/8)）。
- 全部内容见[未关闭的 issue](https://github.com/wuwaforF/local-ai-connector/issues)。

## 文档

- [参考说明](docs/REFERENCE.zh-CN.md)（[英文版](docs/REFERENCE.md)）：工具、配置、身份来源和手动配置。
- [架构](docs/ARCHITECTURE.md)：代码如何组织。
- [平台计划与证据](docs/PLATFORM_PLAN.md)：支持矩阵，以及每项结论是怎样测试的。
- [贡献指南](CONTRIBUTING.md)和[安全策略](SECURITY.md)。安全漏洞请私下报告。
- [研究工具](research/)：可复现的诊断工具，例如 Antigravity 聊天身份探针。

## 许可证

MIT，见 [LICENSE](LICENSE)。第三方声明见 [NOTICE.md](NOTICE.md)。
