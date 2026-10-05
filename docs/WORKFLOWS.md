# 分阶段构建与审阅工作流

工作流把一次由端点所有者编写的计划保存为不可变记录，并按“构建→审阅”顺序交给两个不同的已登记端点。所有者、构建者和审阅者是三个不同端点。它只使用既有聊天和普通连接器通道。每个构建或审阅阶段（含每次修订）都需要独立的普通任务批准；工作流工具本身不批准、不投递任务，也不会延长授权。

候选成果作为连接器中保存的文本返回。流程不会把它应用到工作区、运行命令或测试，也不会把工作者填写的验证说明当成独立测试回执。审阅通过只表示审阅者接受了与其回执中阶段编号和 SHA-256 完全匹配的已存文本。

## 输入和回执格式

`connector_workflow_start` 的参数为：

```json
{
  "plan": "<1–4000 个非空字符的完整计划>",
  "builder": "<已登记的构建端点 ID>",
  "reviewer": "<已登记的审阅端点 ID>",
  "request_key": "<本所有者范围内稳定且唯一的创建编号>",
  "max_revisions": 1
}
```

`max_revisions` 可为 0 到 3，默认 1；最多形成一次初始构建／审阅及三次修订构建／审阅。相同所有者和 `request_key` 的重试必须保持计划、角色和修订上限不变；否则返回幂等冲突。阶段编号由服务生成；该阶段的稳定去重键为 `workflow:<stage_id>`，普通连接器通道仍使用其通道 ID。重试沿用同一阶段意图和去重键，不会另建重复通道。宿主普通委派的确认规则保持有效；采用原生工具许可的端点每次调用 delegate 仍可能再次要求许可，已授权的等待使用 receive。

构建者必须把以下完整 JSON 对象作为该阶段的连接器答案返回，不加 Markdown 围栏、前后说明或其他字段。每个文本字段须为 1–6000 个非空白字符：

```json
{
  "schema_version": 1,
  "run_id": "<status 返回的工作流 ID>",
  "stage_id": "<本阶段 ID>",
  "candidate_content": "<候选文本原文>",
  "verification": "<已执行的检查及限制；这是工作者报告>"
}
```

审阅者收到上游构建阶段、完整候选文本和服务端计算的哈希后，必须返回以下完整对象。`build_stage_id` 和 `candidate_sha256` 必须逐字匹配该候选；`verdict` 只能是 `accept` 或 `revise`；`findings` 为 1–2000 个非空白字符：

```json
{
  "schema_version": 1,
  "run_id": "<工作流 ID>",
  "stage_id": "<当前审阅阶段 ID>",
  "build_stage_id": "<产生该候选的构建阶段 ID>",
  "candidate_sha256": "<候选内容的 SHA-256>",
  "verdict": "accept",
  "findings": "<审阅结论或发现>"
}
```

任一阶段也可返回明确的阻塞回执：

```json
{
  "schema_version": 1,
  "run_id": "<工作流 ID>",
  "stage_id": "<当前阶段 ID>",
  "blocked": "<阻塞原因，1–2000 个非空白字符>",
  "next_action": "<所需的下一步，1–2000 个非空白字符>"
}
```

解析器只接受一个完整 JSON 对象，拒绝重复键、非有限数字、额外字段、字段缺失、身份不符、正文夹带说明及与候选不匹配的审阅。有效阻塞结果终止工作流并保留原因和下一步。格式错误或普通叙述会记录为 `invalid_result`，原始连接器答案仍保留，错误代码记录在阶段上；不会被改写成阻塞回执或自动要求替代结果。`revise` 会把原候选和审阅意见交给下一构建阶段；超过修订上限时工作流以 `revision_limit` 结束。

## 工具顺序

先用 `connector_status` 确认当前 `self`，选择彼此不同、且都不同于该所有者的 `builder` 与 `reviewer`。工具使用认证身份作为所有者；通过 CLI/API 调试时也必须核对所选端点配置与实际工具 `self` 相同。`delegation` 参数不能转移所有权，换用另一发起端会形成另一条普通任务通道。随后调用：

```text
connector_workflow_start(plan, builder, reviewer, request_key, max_revisions=1)
```

响应中的 `id` 是后续工具所需的 `workflow_id`，另包含 `version`、阶段信息以及 `delegation`。使用 `delegation` 中完整的 `target`、`message`、`request_key`、`conversation_mode` 调用现有普通委派批准路径；其中 `conversation_mode` 固定为 `existing`。支持聊天确认的所有者端点使用 `connector_delegate(target, message, request_key, conversation_mode)`。若端点只提供 `connector_request_help`，按其对应字段提交同一请求，并通过现有批准入口审批。批准每次只覆盖本阶段的普通通信通道。

如果宿主无法提供普通任务确认，保留当前结果并如实报告。工作流启动已持久化意图及待批准通道，`connector_workflow_status(workflow_id)` 会继续显示 `waiting` 和 `pending` 通道观察；这不表示任务已批准、投递或执行。不要用模型参数、工作流工具调用或沉默代替批准。

工作者答复后，由所有者显式推进：

```text
connector_workflow_status(workflow_id)
connector_workflow_resume(workflow_id, expected_version=<最近 status 返回的 version>)
```

`resume` 会读取对应通道的实际答复，按上述完整 schema 校验，并持久化阶段结果。构建结果会准备审阅阶段；`revise` 会准备下一构建阶段。返回内容中的下一阶段 `delegation` 仍须经普通批准路径单独批准。没有后台协调器替所有者等待或推进；如调用中断或服务重启，所有者读取状态并以当前 `version` 再次调用 `resume`。版本不匹配时先重新读取状态，不要猜测或重放不同版本。

`connector_workflow_cancel(workflow_id, expected_version=<最近 status 返回的 version>)` 取消等待中的运行并撤销当前阶段通道。取消不删除聊天、消息或文件；若服务在取消和通道登记之间中断，启动时会对已取消运行做通道撤销核对。取消是终态，不能恢复。

## 状态和边界

状态返回中有两类不同记录：顶层 `state` 和 `stages[*].state` 是工作流数据库中持久化的处理状态；`stages[*].channel_observation.status` 是通道中保存的状态，`effective_state` 按当前时间计算到期或空闲关闭状态；状态查询本身不修改通道。工作流为 `waiting` 只表示等待所有者处理当前阶段，并不证明通道有效、已获批或工作者运行。应同时读取通道观察、`binding_matches` 和 `delegation`。`delegation` 只在运行等待且冻结绑定仍匹配时给出。

开始时会冻结构建者和审阅者的当前唤醒目标及原生会话登记。若绑定改变，后续投递或批准会被拒绝，不能静默转发到替代聊天。待批准通道在授权到期、被拒绝或撤销后终止；不会自动换通道或重新委派。`accepted` 表示审阅者接受准确候选；`denied`、`expired`、`revoked`、`worker_blocked`、`invalid_result` 和 `revision_limit` 都须按实际返回状态处理。

本功能不管理工作区文件，不应用候选补丁，不执行命令，不提供独立测试证明，也不自动创建或归档原生聊天。接入状态或自动化回归通过不能代替真实 MCP 宿主中的逐阶段审批和人工验收。

混合收件箱中，失效工作流仅阻断其自身通道；未过滤的 receive 会返回 `blocked_channels` 并继续返回其他有效任务，显式读取失效通道仍返回错误。若混合唤醒批次已明确未送达，可审计隔离失效成员，并用原投递编号和剩余重试预算处理其他成员；结果不明的批次保留覆盖记录，不重放。被隔离成员不会自动重新唤醒，应检查原通道和绑定后人工恢复接收或取消流程。

部署后的运行包已通过独立 stdio 列表验证（participant 共 11 个工具）。现有原生聊天可能仍缓存部署前的工具表；配置文件更新不等于该聊天已重新加载工具。应以该聊天的 MCP 面板及实际工具调用核对，不能只据运行包列表宣称原生加载完成。
