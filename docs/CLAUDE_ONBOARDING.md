# Claude Desktop Code 预创建会话接入

本流程将一个已打开的 Claude Desktop Code 聊天绑定为 `claude_code` 工作者。它支持新建首次唤醒绑定或替换现有目标；不安装连接器服务、运行环境或 Claude 端点，也不创建 Claude 聊天。用户应先在 Claude Desktop 手动创建并打开目标 Code 聊天，按宿主提示完成工作区信任；任务完成后的归档仍由用户在 Claude 中手动操作。

## 开始前：完成一次连接器安装

会话绑定向导要求连接器运行环境、已安装并运行的本机服务、`claude_code` 端点登记及兼容的唤醒配置都已存在。它不能从空数据目录完成首次服务安装或端点登记，也不安装 Python 依赖。首次设置请按 [安装、初始化与启动](../README.zh-CN.md#安装与启动)、[端点登记](../README.zh-CN.md#登记工作者及能力)和 [stdio MCP 配置](../README.zh-CN.md#stdio) 完成现有步骤；唤醒适配器契约见[通用化说明](GENERALIZATION.md#可替换唤醒适配器)。

连接器工作目录必须包含可用的 `.venv/bin/python`。若目标项目尚无 `local_ai_connector` MCP 条目，向导会从当前连接器目录生成条目；此时拒绝使用位于 `~/Documents` 下的运行目录。若项目已有指向独立 Claude 可读运行时的匹配条目，向导会保留并接受该条目。Claude 还须能读写目标项目的 `.mcp.json`。工作区信任、项目 MCP 信任以及宿主原生权限均按 Claude／Codex 界面提示由用户确认，向导不会代为批准。

## Desktop 聊天内会话核对

已实现两个可选 MCP 工具，让用户在已有 Desktop 聊天里按标题和工作区选择 Claude 会话。助手调用 `connector_claude_binding_catalog`，显示编号列表；用户选择后，助手携带该行的 `desktop_id` 和 `selection_revision` 调用 `connector_claude_binding_inspect`。用户不需要复制会话 ID、元数据路径或命令。

这两个工具只读取本机元数据并核对既有绑定，不提交首次绑定或更换目标。返回 `existing_binding_verified` 仅证明所选会话与已安装绑定、配置和私有记录相符，不能证明在线或消息往返成功。其他结果应按以下方式处理：

| 状态 | 下一步 |
| --- | --- |
| `catalog_stale` | 重新列出会话，让用户重新选择；不得沿用旧行版本。 |
| `stale_binding` | 保留原绑定，报告记录或配置已失效；等待受支持的原生重新接入。 |
| `native_authorization_unavailable` | 所选会话尚未绑定；此只读工具不提供提交，尚未请求 Claude 批准。保留原绑定，不能把结果视为宿主拒绝。 |
| `binding_inspection_unavailable` | 核查本机安装或所选行，不声称绑定成功。 |

选择列表中的一行不构成接入授权。对未绑定的目标，工具在读取私有会话记录或地址前停止。已绑定目标会在验证前后检查元数据、配置和绑定是否变化；配置、凭据、记录和消息均不写入，也不连接消息地址。

安装维护者可在现有 stdio 启动参数末尾加入 `--claude-binding-root /absolute/path/to/local-ai-connector` 启用这两个工具；该目录必须与既有 bridge 安装目录匹配。参数由本机安装配置确定，工具调用者不能覆盖目录或端点身份。工具未启用时，原有 MCP 工具集合保持不变。若 Codex 设置了 `enabled_tools`，需同时加入这两个工具名称。此路径复用普通 MCP 工具和已有 Desktop 聊天；不需要额外窗口。

2026-10-03 本机已安装独立运行副本并更新两端 MCP 启动参数。独立客户端按实际启动配置验证成功：Codex 身份为 `gpt`、10 个工具，Claude 身份为 `claude_code`、13 个工具，均能读取会话列表。当前原生聊天仍缓存旧工具；Claude 的 `/mcp` 面板显示 Connected、7 个工具，未发现刷新入口。两端原生聊天发现和实际调用新工具尚待验收，不能把独立客户端结果当作 Desktop 验收完成。Astra 后续核查确认官方 app-server 提供 `config/mcpServer/reload`，但当前工具没有暴露该接口；不能用私有 IPC 绕过桌面控制限制。用户未找到刷新按钮时，先保留待验收状态；其他运行任务结束后，可由用户退出并重新打开 Codex，作为加载新运行配置的尝试，随后核查实际工具。不要承诺仅重新打开聊天会刷新，也不要连带重启 Claude。Codex 的正常连接重载需用户操作；不要为刷新工具重启无关聊天，也不要把原生授权拒绝改写成可重试的接入方式。

2026-10-03 后续现场验证：用户退出并重新打开 Codex 后，本聊天已发现两个新工具。真实 `connector_claude_binding_catalog` 调用返回 `catalog_ready` 和 10 个会话，实际发起身份为 `gpt`。用户明确选择当前绑定后，真实 `connector_claude_binding_inspect` 返回 `existing_binding_verified`：Connector loading test 及其工作区的身份、配置和记录核对通过，`changes=[]`。此检查不证明在线或新消息往返；Claude 原生聊天尚未刷新。此结果不代表新会话绑定或更换目标已完成。

## MCP 提供给模型和 agent 的绑定指引

MCP 的初始化使用说明向各工具配置和语言配置提供终端绑定指引。启用本机 `--claude-binding-root` 时，还公开英文资源 `connector://claude-binding-guide`，内容使用启动配置中的实际安装目录和数据目录，给出已存在的 `deployment/register-claude-code.command` 的显式参数、捕获、预览、确认替换和消息验收步骤。

模型先让用户按标题和工作区选择会话，再读取指南；有终端及文件访问权限、有效当前回执和明确绑定授权时，由模型执行现有脚本。此指南不会替模型启动终端、授予文件权限或生成原生回执。资源读取不可用时，catalog 的 `manual_binding` 提供指南文件路径及安装、数据目录，允许有本机文件工具的 agent 读取同一份指南。没有终端能力时报告确切脚本位置和缺失输入，不要求用户猜会话 ID。

英文指南的单一维护源为 [claude_binding_guide.md](../src/local_ai_connector/claude_binding_guide.md)。它使用显式参数流程；无参数向导会进入此前未完成原生验收的合并 JSON 回执流程，不能作为自动回退。保存记录前先确认没有 pending/active 通道，再核对目标和预览；在已有明确授权内执行，范围变化时才补充确认。端点凭据、其他绑定和原生许可边界保持原样。

更新后的使用说明在新 MCP 连接时加载；当前已连接聊天可能缓存旧说明。指南发布与读取通过不能替代新聊天绑定或实际消息往返验收。

## 显式终端绑定流程（macOS，预览）

以下是供安装维护者使用的终端向导及 CLI 预览流程。Desktop 聊天内的只读会话核对见上文；首次绑定和替换目标仍需受支持的原生授权。

先在 Claude Desktop 中手动打开要绑定的 Code 聊天和对应工作区。然后在已安装的连接器目录运行无参数向导：

```sh
cd "/path/to/local-ai-connector"
./deployment/register-claude-code.command
```

向导列出当前用户本机浅层元数据中未归档的 Code 聊天，显示清理过的标题、工作区和 Desktop ID。请按实际目标聊天和工作区选择编号；标题仅供辨认，不能单独作为身份。向导不会自动选最近聊天或按标题匹配。选错时直接回车取消。

选定后，向导生成一次性英文只读 Bash 提示词，其中含本轮随机 nonce。把提示词复制到刚才选定的聊天，要求 Claude 只运行其中命令、随后停止，不调用连接器工具或改动文件／设置；再把该工具实际输出的完整 JSON 原样粘回向导。回执只含 `schema=1`、nonce、CLI 会话 ID、当前工作目录和 messaging socket 地址，不含连接器凭据或其他秘密；不要手工重建或编辑 JSON。向导会校验 nonce、CLI ID、工作区及 Desktop 元数据，使回执与本轮和所选聊天相符。nonce 只用于关联本次运行，不是来源认证；若宿主拒绝命令或输出不符，按停止条件处理。

本机的 Claude Auto 模式曾以 `[Credential Materialization]` 拒绝这条生成命令；真实宿主是否接受该回执目前未验证。若 Claude 拒绝或警告，停止向导并保留完整的原生结果，不绑定会话。若宿主提供明确的原生人工许可，按其界面决定；不要切换权限模式或规则、拆分或改写命令、重建 JSON，也不要声称回执已被现场验证。

确认前，向导显示目标会话、工作区、原绑定（或首次绑定）以及确切变更文件清单。核对无误后输入 `y` 确认一次；其他输入会取消。向导内部保留会话 ID、路径、私有记录位置和先前绑定摘要，再执行与高级流程相同的校验和回滚部署。首次绑定只在服务、端点及唤醒配置已预先安装时创建。向导保存会话记录、更新 `wakeup.json` 的 Claude 绑定，并在目标工作区的 `.mcp.json` 中添加 `local_ai_connector` 条目；已有匹配条目会复用，冲突时停止，不会覆盖，也不改其他 MCP 条目。唤醒选项不兼容时也会停止。成功后，在 Claude 原生界面处理任何新的项目 MCP 信任提示，并按发起端实际批准流程验收。

本机传统显式流程已现场验证：在选定聊天取得真实、可见的工具地址回执，保存记录、预览并确认替换绑定，随后同一 Claude 通道真实返回 893 和 900。证据见任务中的 `work/claude-runtime-acceptance.json` 和 `outputs/Claude连接器接入验收-20261002.json` 的最终验收。后续合并 nonce、身份、目录和地址的 JSON 快捷回执请求被 Auto 分类器拒绝；该快捷请求没有执行，也不应通过其他提取方法规避拒绝。

Simple testing 的本轮结果来自连接器只读检查器对未绑定目标的本地分支，未申请 Claude 原生批准；不是该聊天实际拒绝接入。用户检查 `/status` 后没有 Peer address，只证明这个显示地址入口不可用。更换到此聊天需要一份通过受支持的人类授权机制取得的真实当前地址回执，随后复用已有捕获、预览和明确替换流程。`/permissions` 在 Desktop Code 标签不可用，不将通用 CLI 的 Recently denied 说明当成桌面入口。旧聊天的批准或旧地址不能代替新目标的回执。

## 高级：手动捕获与绑定

下文为保留的显式 CLI 操作。需要自动化或逐项控制时，可直接使用这些参数。已有命令参数保持支持：首次绑定时，预览和应用命令都加 `--create-binding`；替换已有绑定时，预览获得的摘要用 `--replace-binding` 在复核和应用命令中原样传回。

### 身份和目录要求

Claude Desktop 元数据同时保存两种身份：`sessionId` 是 Desktop 会话标识（通常形如 `local_<UUID>`），`cliSessionId` 是 Code 引擎 UUID。命令中的 `--desktop-id` 必须取前者，`--cli-id` 必须取后者；两者须与所选聊天元数据中的值完全相符。`--metadata` 必须指向以 Desktop 标识命名的那个元数据 JSON 文件，不能猜 CLI 文件名或从聊天标题推断身份。`--workspace` 是绝对路径，并且当前实现要求元数据的 `cwd` 和 `originCwd` 都与它逐字相同；由 worktree 启动、使两者不同的会话不适用此流程。

将下例占位值替换为实际值。路径都应为绝对路径并保留 shell 引号；`<...>` 是说明性占位符，不要原样输入。会话元数据路径和 ID 必须来自用户明确选定的聊天及其本机 Claude 元数据，不能自动猜测或改选其他目标。

```sh
cd "/absolute/path/to/local-ai-connector"
desktop_id="<Desktop sessionId, for example local_<UUID>>"
cli_id="<Code cliSessionId UUID>"
workspace="/absolute/path/to/selected/workspace"
metadata="/absolute/path/to/Claude/Desktop/metadata/<desktop-id>.json"
data="$HOME/.local/share/local-ai-connector"
record="$data/$cli_id.json"
```

先在这个选定聊天内读取 `/status`，保留它实际显示的 `Peer address` 作为地址来源记录。若 Desktop 没显示地址，可在同一聊天的终端执行 `printf '%s\n' "$CLAUDE_CODE_MESSAGING_SOCKET"` 并保留实际输出。只可使用该聊天明确显示的地址；不得枚举、搜索、猜测或选用其他 Unix socket。后续命令中的 `socket` 必须替换成这份记录里的真实地址。

### 捕获和刷新绑定

地址捕获与唤醒绑定是两个单独步骤。捕获命令默认只预览；检查输出、确认目标和地址后，显式加 `--apply` 才会写入会话记录。记录包含选定 CLI 会话、工作区及 socket 的设备号和 inode；通过临时文件原子替换，权限为仅当前用户可读写（0600）。捕获不会修改绑定或重启服务。

```sh
".venv/bin/python" "integrations/claude_code/onboarding.py" \
  --metadata "$metadata" --desktop-id "$desktop_id" --cli-id "$cli_id" \
  --workspace "$workspace" --session-record "$record" \
  --capture-socket "/actual/address/from/selected/chat"

# 确认上一步预览对应所选聊天和地址后，才写入记录：
".venv/bin/python" "integrations/claude_code/onboarding.py" \
  --metadata "$metadata" --desktop-id "$desktop_id" --cli-id "$cli_id" \
  --workspace "$workspace" --session-record "$record" \
  --capture-socket "/actual/address/from/selected/chat" --apply
```

然后运行普通预览，查看 `binding_changes`、`previous_binding_sha256`、`service_running`、`runtime_idle` 和 `mcp_available`：

```sh
".venv/bin/python" "integrations/claude_code/onboarding.py" \
  --metadata "$metadata" --desktop-id "$desktop_id" --cli-id "$cli_id" \
  --workspace "$workspace" --session-record "$record" --data "$data"
```

检查预览中的 `wake_compatibility_issues`。当前实现要求连接器唤醒配置显式设置 `enabled=true`、`send_when_unknown=true`，并让 `claude_code` 绑定的 `restore=false`。Claude bridge 的状态查询是 `unknown`，因此未知状态时必须允许发送；bridge 不支持恢复操作。若有不兼容项，应用会拒绝，且不会替用户静默修改这些设置。先在配置中明确修正，再重新预览并检查完整报告。

应用前会用运行时配置加载器完整解析候选配置并检查兼容性；检查失败时尚未修改配置。应用后，回滚就绪检查会重新加载已安装配置并再次验证兼容性及目标绑定；若这项检查失败，部署会恢复原配置和服务。

如果 `binding_changes` 为 `true`，必须把这次预览给出的 `previous_binding_sha256` 原样作为 `--replace-binding`，再单独预览一次核对；只有确认摘要仍精确匹配要替换的旧绑定后，才加 `--apply`：

```sh
old_binding_sha256="<previous_binding_sha256 from the reviewed preview>"
".venv/bin/python" "integrations/claude_code/onboarding.py" \
  --metadata "$metadata" --desktop-id "$desktop_id" --cli-id "$cli_id" \
  --workspace "$workspace" --session-record "$record" --data "$data" \
  --replace-binding "$old_binding_sha256"

# 复核输出中的目标和 binding_changes 后才应用同一摘要：
".venv/bin/python" "integrations/claude_code/onboarding.py" \
  --metadata "$metadata" --desktop-id "$desktop_id" --cli-id "$cli_id" \
  --workspace "$workspace" --session-record "$record" --data "$data" \
  --replace-binding "$old_binding_sha256" --apply
```

更新只改 `wakeup.json` 中 `claude_code` 的唤醒绑定；已有凭据、端点及其他绑定保持原值。存在待处理或活动通道时拒绝变更。若绑定已经一致，应用是 no-op：不会因无变化而重启服务，但要求配置的连接器监听器已经运行。完成后仍须在真实 Claude MCP 会话里做实际请求—响应往返；配置文件、socket 记录、监听进程或 `mcp_available=requires_live_test` 都不能单独证明 MCP 已就绪。

## 文件访问限制

已观察到 Claude 报错 `Couldn’t access Documents` 和 `Couldn’t access this folder`；此前在该受保护位置上的 CLI 读取或 Bash 也返回 `EPERM`／`Operation not permitted`。Claude 必须能读取选定工作区、连接器运行时及项目 MCP 配置。只把工作区移出 Documents 仍不够：如果项目 `.mcp.json` 的可执行文件或 `PYTHONPATH` 仍指向 Documents，访问仍会失败。后续可准备一份位于 Claude 可读非受保护目录的普通应用代码和依赖快照，并让项目本地 MCP 配置指向该运行时及现有私有端点；部署这类快照前应先准备并审阅其确切内容。本说明不建议关闭 macOS 隐私保护或绕过系统权限。若 Claude 显示上述错误，应报告原始错误并修复目标路径的可读性。

本说明描述代码中的预览和应用流程，不代表某台机器已完成部署，也不代表真实 MCP 往返已通过。


## 独立运行副本的维护与验收

若采用非 Documents 运行副本，应使用现有基础 Python 在新的目录创建 venv，复制已安装依赖和 `src/local_ai_connector` 包，并排除指向旧源码目录的 editable `.pth` 文件及缓存。不要复制旧 venv 的启动脚本或把 `PYTHONPATH` 指回 Documents。验证新 Python 的实际路径、`sys.path` 和连接器、MCP 等依赖的导入路径均位于可读目录，再让所选工作区的项目 `.mcp.json` 指向新运行副本及原有私有端点配置。保存源码文件摘要，便于核对运行版本；不修改端点凭据。

源码更新后，运行副本不会自动更新。应先完成源码测试，再准备一个新的版本副本并验证导入路径；核对项目配置后切换运行副本，重新加载 Claude 会话的 MCP。CLI 会话或消息地址变化时，重新读取该聊天的元数据和地址，按上述流程捕获和复核，不沿用旧地址。若显示新的信任或权限确认，应由用户处理。

MCP 面板显示 Connected、`connector_status` 实际调用成功和重复绑定 no-op，只证明接入及绑定检查通过。最终验收仍需从发起聊天正常委派，在实际批准后验证一次回答及同一通道、同一 Claude 聊天的续问。若发起端返回 `approval_timeout`，任务尚未批准，不应绕过确认或声称投递成功；通过正式撤销操作清理本次测试通道并保留审计记录。用户可用时发起新的验收请求，不复用已撤销的测试编号。Claude 聊天仍按约定手动归档。
