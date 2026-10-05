"""A task-facing operation over the existing consent-bound message transport."""
import asyncio
import hashlib
import math

from mcp.shared.message import ServerMessageMetadata
from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from .core import ConnectorError, require


async def delegate_with_host_permission(ctx, call, decide, target, message, request_key, *, reply_to=None, reply=None,
                                        conversation_mode="existing", archive_source=None, selected_conversation=None, binding_only=False):
    """Only for an explicitly configured host enforcing mandatory tool permission."""
    require(reply_to is None and reply is None, "invalid_reply",
            "Continue an approved channel through connector_send and connector_receive.")
    # The configured host gates tools/call before arrival. This is not an
    # elicitation receipt and must be audited as a different trust boundary.
    if archive_source is not None:
        task = await call(ctx, action="archive_open", key=request_key, **archive_source)
    else:
        task = await call(ctx, action="open", target=target, body=message, key=request_key,
                          ttl_seconds=3600, idle_seconds=120, conversation_mode=conversation_mode,
                          **({'binding_only': True} if binding_only else {}),
                          **({'selected_conversation': selected_conversation} if selected_conversation is not None else {}))
    conversation = task.get('conversation', {'mode': conversation_mode, 'created': False})
    if task["status"] == "pending":
        task = await decide(ctx, task["id"], "accept")
    if task["status"] == "closed":
        result = await call(ctx, action="result", channel=task["id"])
        return {**result, "request_key": request_key, "next_tool": None}
    return {"state": task["status"], "channel": task["id"], "request_key": request_key,
            "conversation": conversation,
            "next_tool": "connector_receive" if task["status"] == "active" else None,
            "detail": "Continue this channel with receive/send/finish. Stored messages do not prove worker execution."}


async def continue_task(ctx, call, channel, message, request_key, timeout_seconds=180, *, reply_to=None, reply=None,
                        mcp_locale="zh-CN"):
    if mcp_locale not in ("zh-CN", "en-US"):
        raise ValueError("mcp_locale must be zh-CN or en-US")
    english = mcp_locale == "en-US"
    require(type(timeout_seconds) is int and 1 <= timeout_seconds <= 600,
            "invalid_wait", "Execution waits must be between 1 and 600 seconds." if english else "执行等待时间须为 1 至 600 秒")
    require((reply_to is None) == (reply is None), "invalid_reply",
            "A clarification response requires both reply_to and reply." if english else "补充回复须同时提供 reply_to 和 reply")
    if reply_to is not None:
        require(isinstance(reply_to, str) and 0 < len(reply_to) <= 200 and isinstance(reply, str)
                and 0 < len(reply.strip()) <= 16000, "invalid_reply",
                "A clarification response requires a valid question ID and 1 to 16000 characters." if english
                else "补充回复须提供有效问题编号和 1 至 16000 字符正文")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    question_id = None
    try:
        async with asyncio.timeout(timeout_seconds):
            task = await call(ctx, action="continue", channel=channel, body=message, key=request_key)
            question_id = task["id"]
            cursor = task["seq"]
            if reply_to is not None:
                reply_key = "continue-answer:" + hashlib.sha256((request_key + "\0" + reply_to).encode()).hexdigest()
                await call(ctx, action="send", channel=channel, kind="answer", body=reply, key=reply_key,
                           reply_to=reply_to)
            while True:
                result = await call(ctx, action="round_result", channel=channel, question_id=question_id)
                if result["questions"]:
                    return {"state": "input_required", "channel": channel, "question_id": question_id,
                            "questions": result["questions"], "answer": result["answer"]}
                if result["answer"]:
                    if result["state"] == "active":
                        try:
                            await call(ctx, action="finish", channel=channel)
                        except ConnectorError as exc:
                            if exc.code != "unanswered_questions":
                                raise
                            current = await call(ctx, action="round_result", channel=channel,
                                                 question_id=question_id)
                            if current["questions"]:
                                return {"state": "input_required", "channel": channel,
                                        "question_id": question_id, "questions": current["questions"],
                                        "answer": current["answer"]}
                            if current["answer"] is None:
                                return {"state": current["state"], "channel": channel,
                                        "question_id": question_id, "request_key": request_key}
                    return {"state": "completed", "channel": channel, "question_id": question_id,
                            "answer": result["answer"], "conversation": result.get("conversation", {"mode": "existing", "created": False}),
                            "next_round": {"tool": "connector_continue", "channel": channel}}
                if result["state"] not in ("active", "closed"):
                    return {"state": result["state"], "channel": channel, "question_id": question_id,
                            "request_key": request_key}
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return {"state": "running", "channel": channel, "question_id": question_id,
                            "request_key": request_key, "detail": "Resume with the same channel, message, and request_key."}
                received = await call(ctx, action="receive", channel=channel, after=cursor,
                                      timeout=min(20, math.ceil(remaining)))
                cursor = received["cursor"]
                if received["state"] not in ("active", "waiting"):
                    return {"state": received["state"], "channel": channel, "question_id": question_id,
                            "request_key": request_key}
    except TimeoutError:
        return {"state": "running", "channel": channel, "question_id": question_id,
                "request_key": request_key, "detail": "Resume with the same channel, message, and request_key."}


async def delegate(ctx, call, decide, target, message, request_key, timeout_seconds=180, *, reply_to=None, reply=None,
                   mcp_locale="zh-CN", conversation_mode="existing", archive_source=None, selected_conversation=None, binding_only=False):
    if mcp_locale not in ("zh-CN", "en-US"):
        raise ValueError("mcp_locale must be zh-CN or en-US")

    def text(chinese, english):
        return english if mcp_locale == "en-US" else chinese

    require(type(timeout_seconds) is int and 1 <= timeout_seconds <= 600,
            "invalid_wait", text("执行等待时间须为 1 至 600 秒", "Execution waits must be between 1 and 600 seconds."))
    require((reply_to is None) == (reply is None), "invalid_reply",
            text("补充回复须同时提供 reply_to 和 reply", "A follow-up response requires both reply_to and reply."))
    if reply_to is not None:
        require(isinstance(reply_to, str) and 0 < len(reply_to) <= 200 and
                isinstance(reply, str) and 0 < len(reply.strip()) <= 16000,
                "invalid_reply", text("补充回复须提供有效问题编号和 1 至 16000 字符正文",
                                      "A follow-up response requires a valid question ID and 1 to 16000 characters of text."))
    caps = ctx.client_capabilities
    elicitation = caps.elicitation if caps else None
    modern = ctx.protocol_version in MODERN_PROTOCOL_VERSIONS
    require(elicitation is not None and (elicitation.form is not None or elicitation.url is None)
            and (modern or ctx.session.can_send_request),
            "host_confirmation_unavailable", text("此连接不支持聊天内确认；任务未投递，请检查宿主的 form elicitation 支持",
                                                  "This connection does not support confirmation in chat. The task was not delivered; check the host's form elicitation support."))

    # Fixed consent lifetime makes replay independent of the caller's current wait budget.
    if archive_source is not None:
        task = await call(ctx, action="archive_open", key=request_key, **archive_source)
    else:
        task = await call(ctx, action="open", target=target, body=message, key=request_key,
                          ttl_seconds=3600, idle_seconds=120, conversation_mode=conversation_mode,
                          **({'binding_only': True} if binding_only else {}),
                          **({'selected_conversation': selected_conversation} if selected_conversation is not None else {}))
    channel = task["id"]
    if reply_to is not None:
        require(task["status"] in ("active", "closed"), "invalid_state",
                text("补充回复需要已批准的原任务", "A follow-up response requires an approved original task."))
    if task["status"] == "pending":
        await ctx.report_progress(0, message=text("等待本次任务确认", "Waiting for confirmation of this task."))
        prompt = text(
            f"委派给工作者：{task['responder']}\n\n任务原文：\n{task['original']}\n\n"
            "点击提交即批准本次任务通信；拒绝或取消不会投递。\n\n"
            f"任务编号：{channel}\n本次授权有效期为创建后 1 小时，仅用于本通道的任务通信；"
            "工作者自身的文件、命令等权限仍按其宿主设置执行。",
            f"Delegate to worker: {task['responder']}\n\nOriginal task:\n{task['original']}\n\n"
            "Click Submit or Approve to authorize this task communication. Decline or Cancel will not deliver it.\n\n"
            f"Task ID: {channel}\nAuthorization expires one hour after this task was created and applies only to "
            "task communication on this channel. The worker's host settings still govern permissions for files, commands, and other actions."
        )
        prompt += text(
            "\n\n聊天方式：" + ("在目标应用新建独立聊天，并将本通道的后续轮次绑定该聊天。" if conversation_mode == "new" else
                                "使用已登记的既有聊天；本操作不会新建应用聊天。"),
            "\n\nConversation: " + ("Create a new native chat and keep subsequent channel rounds in that chat." if conversation_mode == "new" else
                                    "Use the registered existing chat. This does not create a native chat."))
        selected = task.get('conversation', {}).get('selected')
        if selected is not None:
            prompt += text(
                f"\n\n选定 Codex 聊天：{selected['title']}\n聊天 ID：{selected['thread_id']}\n工作区：{selected['workspace']}\n"
                "批准仅把本通道及续问绑定到此聊天；专用入口、凭据及工作区配置保持原样。",
                f"\n\nSelected Codex chat: {selected['title']}\nThread ID: {selected['thread_id']}\nWorkspace: {selected['workspace']}\n"
                "Approval binds only this task channel and its follow-ups to this chat. Preserve the ingress, credentials and workspace configuration.")
        if archive_source is not None:
            prompt = text(
                f"归档对话并解除活动登记\n\n目标应用：{task['responder']}\n原任务通道：{archive_source['channel']}\n"
                f"原生对话 ID：{archive_source['conversation_id']}\n\n"
                "提交即批准归档该对话；宿主确认成功后移除其活动路由和登记，保留聊天内容与项目文件。"
                "归档后原通道不能继续发送消息。拒绝或取消不会执行归档。本次批准独立于原任务通信授权，有效期为一小时。",
                f"Archive conversation and remove its active registration\n\nHost: {task['responder']}\n"
                f"Original channel: {archive_source['channel']}\nNative conversation ID: {archive_source['conversation_id']}\n\n"
                "Submit authorizes archiving this chat. After the host confirms success, remove its active route and registration. "
                "Preserve the chat contents and project files. The original channel cannot send further messages. "
                "Decline or Cancel performs no archive. This separate approval expires after one hour.")
        if binding_only:
            selected = task['conversation']['selected']
            prompt = text(
                f"保存 Codex 协作目标\n\n发起端点：{task['requester']}\n接收入口：{task['responder']}\n"
                f"目标聊天：{selected['title']}\n聊天 ID：{selected['thread_id']}\n工作区：{selected['workspace']}\n\n"
                "批准仅保存此发起端点的目标选择，不发送任务，也不授予后续任务权限。后续每个新任务仍须在发起端批准。"
                "选择可跨任务保留，原有任务通道不变。拒绝或取消不改变已保存的目标。",
                f"Save Codex collaboration target\n\nInitiating endpoint: {task['requester']}\nReceiving ingress: {task['responder']}\n"
                f"Selected chat: {selected['title']}\nThread ID: {selected['thread_id']}\nWorkspace: {selected['workspace']}\n\n"
                "Approval saves this initiating endpoint's target preference. It sends no task and grants no future task permissions. "
                "Each new task still requires initiating-host approval. The routing preference persists across tasks; existing task channels stay unchanged. "
                "Decline or Cancel does not change the saved target.")
        try:
            schema = {"type": "object", "properties": {}}
            # Some hosts route forms through the parent tool's progress callback.
            # Forward only that routing token; it never grants task authority.
            token = (ctx.request_context.meta or {}).get("progress_token")
            form = ElicitRequest(params=ElicitRequestFormParams(
                message=prompt, requested_schema=schema,
                meta={"progress_token": token} if token is not None else None))
            if modern:
                responses = ctx.input_responses or {}
                if "approval" not in responses:
                    return InputRequiredResult(
                        input_requests={"approval": form},
                        request_state=channel,
                    )
                # MCPServer authenticates and binds the echoed state to these tool
                # arguments before exposing it; model arguments cannot grant consent.
                require(ctx.request_state == channel and isinstance(responses["approval"], ElicitResult),
                        "invalid_confirmation", text("宿主确认未关联到本次任务，未批准任务",
                                                     "The host confirmation is not bound to this task. The task was not approved."))
                confirmation = responses["approval"]
            else:
                async with asyncio.timeout(300):
                    # Preserve host metadata; a policy cancellation is not a user click.
                    confirmation = await ctx.session.send_request(
                        form, ElicitResult,
                        metadata=ServerMessageMetadata(related_request_id=ctx.request_id))
        except TimeoutError:
            current = await call(ctx, action="result", channel=channel)
            if current["state"] == "pending":
                return {"state": "approval_timeout", "channel": channel, "request_key": request_key,
                        "detail": text("确认等待已到时，任务仍待批准；恢复时复用 request_key",
                                       "The confirmation wait timed out. The task still needs approval; reuse request_key to resume.")}
            if current["state"] not in ("active", "closed"):
                return current
            task["status"] = current["state"]
        else:
            decision = confirmation.action
            if decision == "accept":
                require(confirmation.content is None or confirmation.content == {},
                        "invalid_confirmation", text("宿主确认响应无效，未批准任务",
                                                     "The host returned an invalid confirmation response. The task was not approved."))
            require(decision in ("accept", "decline", "cancel"), "invalid_confirmation",
                    text("宿主确认响应无效", "The host returned an invalid confirmation response."))
            try:
                task = await decide(ctx, channel, decision)
            except ConnectorError as exc:
                if exc.code != "invalid_state" or decision == "accept":
                    raise
                current = await call(ctx, action="result", channel=channel)
                return {**current, "decision": decision, "decision_applied": False,
                        "detail": text("通道状态已变化，本次决定未生效；请以返回的实际状态为准",
                                       "The channel state changed, so this decision was not applied. Use the actual state returned here.")}
            if decision != "accept":
                return {"state": task["status"], "channel": channel, "decision": decision,
                        "host_response_meta": confirmation.meta,
                        "detail": text("宿主未确认任务；decision 及元数据为宿主返回值，不能据此认定用户手动操作",
                                       "The host did not confirm the task. The decision and metadata came from the host and do not establish that the user acted manually.")}

    if binding_only:
        return await call(ctx, action='result', channel=channel)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    cursor = 0
    running = {"state": "running", "channel": channel, "request_key": request_key,
               "detail": text("本次等待已到时，尚未确认完成；复用原任务内容、request_key 及本次补充回复继续等待",
                              "This wait timed out before completion was confirmed. Reuse the original task content, request_key, and any follow-up response to keep waiting.")}
    try:
        async with asyncio.timeout(timeout_seconds):
            if task["status"] in ("active", "closed"):
                await ctx.report_progress(1, message=text("任务已确认，等待工作者的真实回复",
                                                          "The task is approved. Waiting for the worker's actual reply."))
            if reply_to is not None and task["status"] == "active":
                # The broker validates the original question's channel and sender, and
                # its message key preserves one answer across interrupted tool retries.
                await call(ctx, action="send", channel=channel, kind="answer", body=reply,
                           key=f"delegate-reply:{reply_to}", reply_to=reply_to)
            while True:
                result = await call(ctx, action="result", channel=channel)
                if result.get("answer"):
                    finished = result["state"] == "closed"
                    if result["state"] == "active":
                        try:
                            await call(ctx, action="finish", channel=channel)
                        except ConnectorError as exc:
                            if exc.code != "unanswered_questions":
                                raise
                            cursor = 0
                            while True:
                                received = await call(ctx, action="receive", channel=channel, after=cursor, timeout=0)
                                if received["state"] not in ("active", "waiting"):
                                    return {"state": received["state"], "channel": channel}
                                cursor = received["cursor"]
                                questions = [m for m in received["messages"] if m["kind"] == "question" and not m["resolved"]]
                                if questions:
                                    return {"state": "input_required", "channel": channel, "questions": questions,
                                            "answer": result["answer"],
                                            "detail": text("通道仍有未完成问题；用 reply_to 和 reply 补充回复",
                                                           "This channel still has unanswered questions. Use reply_to and reply to respond.")}
                                if not received["messages"]:
                                    break
                            # Another requester may have answered while pages were read.
                            try:
                                await call(ctx, action="finish", channel=channel)
                            except ConnectorError as exc:
                                if exc.code != "unanswered_questions":
                                    raise
                            else:
                                finished = True
                        else:
                            finished = True
                    if finished:
                        await ctx.report_progress(2, message=text("工作者已回复", "The worker has replied."))
                        return {"state": "completed", "channel": channel, "answer": result["answer"],
                                "conversation": result.get("conversation", {"mode": "existing", "created": False}),
                                "next_round": None if archive_source is not None else {"tool": "connector_continue", "channel": channel}}
                if result["state"] not in ("pending", "active"):
                    return result
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return running
                received = await call(ctx, action="receive", channel=channel, after=cursor,
                                      timeout=min(20, math.ceil(remaining)))
                cursor = received["cursor"]
                questions = [m for m in received["messages"] if m["kind"] == "question" and not m["resolved"]]
                if questions:
                    return {"state": "input_required", "channel": channel,
                            "questions": questions,
                            "detail": text("工作者需要补充信息；用 reply_to 和 reply 回复对应问题",
                                           "The worker needs more information. Use reply_to and reply to answer the corresponding question.")}
                if not received["messages"] and received["state"] == "waiting":
                    await ctx.report_progress(1, message=text("工作者仍在执行，继续等待", "The worker is still running. Continuing to wait."))
    except TimeoutError:
        return running
