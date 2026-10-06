# 来源与许可证

本项目为独立实现，没有复制 Workflow Hub 的项目代码或生产数据，也没有打包桌面应用的实现代码。

## 使用的运行依赖

| 依赖 | 本次验证版本 | 许可证 |
| --- | --- | --- |
| 官方 MCP Python SDK / mcp-types | 2.2.0 | MIT |
| Starlette | 1.6.0 | BSD-3-Clause |
| Uvicorn | 0.53.0 | BSD-3-Clause |
| HTTPX | 0.28.1 | BSD-3-Clause |
| HTTPX2（MCP 依赖） | 2.13.0 | BSD-3-Clause |
| Pydantic | 2.13.5 | MIT |
| tomlkit（编辑 Codex 配置并保留注释） | 0.15.1 | MIT |
| pywin32（仅 Windows：文件访问控制检查） | 312 | PSF-2.0 |

完整依赖版本与包哈希保存在 `uv.lock`。各依赖保留其独立许可证；主体 MIT 许可证不替代第三方条款。

## 复用依据

- [OpenRig](https://github.com/mvschwarz/openrig/blob/595b705f2d048a9f157dce53b87e523b029023db/README.md)，参考提交 `595b705f2d048a9f157dce53b87e523b029023db`，Apache-2.0（[该提交的许可证](https://github.com/mvschwarz/openrig/blob/595b705f2d048a9f157dce53b87e523b029023db/LICENSE)）：仅借鉴明确工作所有者、阶段交接和对准确候选进行独立审阅的概念；本项目以现有批准通道独立实现，没有复制 OpenRig 代码，也未安装 OpenRig、采用其 tmux 运行时或依赖其运行组件。
- [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)：直接使用其请求、工具定义、stdio 和取消支持。当前 2.x 采用 `MCPServer` API。
- [Codex MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)：用于配置字段与等待超时的依据。桌面是否加载配置另以本机实测为准。
- [ZCode MCP 文档](https://zcode.z.ai/en/docs/mcp-services)：项目配置位置和 stdio 接入依据。`timeoutMs` 来自本机 3.11.2 附带运行时的调用代码，并有桌面实测。
- [codex-mcp-bridge](https://github.com/buidangminh23/codex-mcp-bridge)，研究提交 `c43b3565ab8757bf247a397be8dddae1f4a1ef35`，MIT：只读研究原生管道和宿主任务元数据。本机独立进程被宿主身份检查拒绝，未采用这条管道。
- [zcode-acp](https://github.com/william0wang/zcode-acp)，研究提交 `8f61b4ab4fb26371760e623c7578fc7b55b83898`，Apache-2.0：核查其为独立 app-server 的适配层；未将其命令行会话等同于桌面会话。
- [codex-supervisor-mcp](https://github.com/redmikarimo/codex-supervisor-mcp)：复用既有调研中事件等待与游标去重的思路，未引入整体依赖。
- [Emdash](https://github.com/generalaction/emdash)：复用既有 worktree 生命周期调研，首版直接使用 Git 原生命令。
- [Ollama 兼容接口](https://docs.ollama.com/api/openai-compatibility)：用于真实模型接入、JSON mode 及可选思考控制。
- [Qwen3 1.7B](https://huggingface.co/Qwen/Qwen3-1.7B)，Apache-2.0：仅作为本机中文测试候选。模型由 Ollama 独立下载，本项目不分发权重。
