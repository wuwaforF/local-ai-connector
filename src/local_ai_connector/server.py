from contextlib import asynccontextmanager
import asyncio
import hmac
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import Broker, ConnectorError
from .mcp_server import create_mcp


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["open", "archive_open", "send", "receive", "finish", "status", "result", "continue", "round_result",
                    "workflow_start", "workflow_status", "workflow_resume", "workflow_cancel", "codex_binding_catalog", "codex_bound_chat"]
    channel: str | None = None
    target: str = ""
    body: str = Field(default="", max_length=16000)
    key: str = Field(default="", max_length=200)
    kind: Literal["question", "answer"] = "question"
    reply_to: str | None = None
    question_id: str | None = None
    after: int = Field(default=0, ge=0)
    timeout: int = Field(default=20, ge=0, le=3600)
    ttl_seconds: int = Field(default=3600, ge=10, le=86400)
    idle_seconds: int = Field(default=120, ge=1, le=3600)
    conversation_mode: Literal["existing", "new"] = "existing"
    conversation_id: str | None = None
    workflow_id: str | None = None
    builder: str = ""
    reviewer: str = ""
    max_revisions: int = Field(default=1, ge=0, le=3)
    expected_version: int = Field(default=0, ge=0)
    selected_conversation: dict | None = None
    binding_only: bool = False
    cursor: str | None = Field(default=None, max_length=4096)


class MCPAuthMiddleware:
    def __init__(self, app, authenticate, generic_peers):
        self.app = app
        self.authenticate = authenticate
        self.generic_peers = generic_peers

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            try:
                peer = self.authenticate(request)
                if peer not in self.generic_peers:
                    raise ConnectorError("native_stdio_required", "原生任务绑定端点须使用 stdio；HTTP 接入请创建独立 generic 端点")
                request.state.peer = peer
            except ConnectorError as exc:
                status = 401 if exc.code == "unauthorized" else 403
                response = JSONResponse({"error": exc.code, "message": str(exc)}, status_code=status)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(data: Path):
    config = json.loads((data / "server.json").read_text())
    approval_tokens = config.get("approval_tokens", {})
    if (not isinstance(approval_tokens, dict) or
            any(peer not in config["peers"] or not isinstance(token, str) or not token
                for peer, token in approval_tokens.items()) or
            len(set(approval_tokens.values())) != len(approval_tokens) or
            any(token == config["admin_token"] or token in config["peers"].values()
                for token in approval_tokens.values())):
        raise ValueError("approval_tokens 必须为已登记端点配置独立且唯一的非空凭据")
    conversations = config.get("conversations", {})
    if not isinstance(conversations, dict) or any(peer not in config["peers"] for peer in conversations):
        raise ValueError("conversations must register existing endpoints")
    codex_catalog = None
    if config.get('codex_binding_cli') is not None:
        from .codex_binding import CodexCatalog
        codex_catalog = CodexCatalog(config['codex_binding_cli'])
    broker = Broker(data / "state.sqlite3", conversations=conversations, codex_catalog=codex_catalog)
    for peer, token in config["peers"].items():
        broker.register(peer, token, config.get("peer_profiles", {}).get(peer))

    def workflow_identity(peer):
        target = None
        if (data / "wakeup.json").exists():
            from .wakeup import load_config
            try:
                bindings, options = load_config(data, set(config["peers"]))
            except (ValueError, OSError) as exc:
                raise ConnectorError("workflow_binding_invalid", "Cannot verify configured workflow worker binding") from exc
            if peer in bindings:
                target = bindings[peer].target
        return {"wake_target": target, "native_registration": broker.conversations.registrations.get(peer)}

    broker.workflows.identity = workflow_identity

    generic_peers = set()
    for peer in config["peers"]:
        path = data / f"{peer}.json"
        client = json.loads(path.read_text()).get("client", "generic")
        if client == "generic":
            generic_peers.add(peer)

    @asynccontextmanager
    async def lifespan(app):
        wakeup = None
        native = native_task = None
        try:
            await broker.workflows.recover_cancelled()
            if (data / "wakeup.json").is_file():
                # Optional adapter: imported only when configured; start() reports its own
                # configuration failures as incidents and leaves the connector running.
                from .wakeup import start
                wakeup = app.state.wakeup = start(data, broker, set(config["peers"]))
            if any(entry["provider"] == "antigravity_sidecar" for entry in conversations.values()):
                from .native_host import NativeHost
                native = app.state.native_host = NativeHost(broker)
                native_task = asyncio.create_task(native.run())
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            if native_task is not None:
                native_task.cancel()
                try:
                    await native_task
                except asyncio.CancelledError:
                    pass
                await native.close()
            if wakeup is not None:
                await wakeup.stop()
            broker.close()

    def auth(req, admin=False, approval=False):
        # Browser-origin requests cannot administer or invoke the local service.
        if "origin" in req.headers:
            raise ConnectorError("forbidden", "仅接受本地客户端请求")
        value = req.headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise ConnectorError("unauthorized", "缺少本地凭据")
        token = value[7:]
        if approval:
            for peer, expected in approval_tokens.items():
                if hmac.compare_digest(token.encode(), expected.encode()):
                    return peer
            raise ConnectorError("unauthorized", "宿主审批凭据无效")
        if admin:
            if not hmac.compare_digest(token, config["admin_token"]):
                raise ConnectorError("forbidden", "仅本机管理入口可以批准连接")
            return None
        return broker.authenticate(token)

    async def body(req):
        payload = bytearray()
        async for chunk in req.stream():
            payload.extend(chunk)
            if len(payload)>100000:
                raise ConnectorError("payload_too_large", "请求过大")
        try:
            return json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise ConnectorError("invalid_json", "请求必须为 JSON")

    def operation(payload):
        try:
            return Operation.model_validate(payload)
        except ValidationError:
            raise ConnectorError("invalid_request", "操作参数无效，请检查字段和类型")

    async def execute(peer, op):
        if op.selected_conversation is not None and op.action != 'open':
            raise ConnectorError('invalid_request', 'Selected-chat input is only valid for initial delegation.')
        if op.binding_only and op.action != 'open':
            raise ConnectorError('invalid_request', 'Saving a target is only valid for an initial binding request.')
        if op.action == 'codex_binding_catalog':
            registration = broker.conversations.registrations.get(op.target, {})
            if broker.codex_catalog is None or registration.get('provider') != 'codex_ingress':
                raise ConnectorError('codex_binding_unavailable', 'Codex quick binding is not enabled.')
            return await broker.codex_catalog.catalog(registration['session_id'], cursor=op.cursor)
        elif op.action == 'codex_bound_chat':
            return await broker.saved_codex_target(peer, op.target)
        elif op.action == "workflow_start":
            if op.conversation_mode != "existing":
                raise ConnectorError("workflow_conversation_unsupported", "Workflows use existing chats only; no new chat was substituted")
            return await broker.workflows.start(peer, op.body, op.builder, op.reviewer, op.key, op.max_revisions)
        elif op.action == "workflow_status":
            return broker.workflows.status(peer, op.workflow_id)
        elif op.action == "workflow_resume":
            return await broker.workflows.resume(peer, op.workflow_id, op.expected_version)
        elif op.action == "workflow_cancel":
            return await broker.workflows.cancel(peer, op.workflow_id, op.expected_version)
        elif op.action == "open":
            return await broker.open(peer,op.target,op.body,op.key,op.ttl_seconds,op.idle_seconds,op.conversation_mode,
                                     selected_conversation=op.selected_conversation, binding_only=op.binding_only)
        elif op.action == "archive_open":
            return await broker.archive_open(peer, op.channel, op.conversation_id, op.key)
        elif op.action == "continue":
            return await broker.continue_channel(peer, op.channel, op.body, op.key)
        elif op.action == "send":
            return await broker.send(peer,op.channel,op.kind,op.body,op.key,op.reply_to)
        elif op.action == "receive":
            return await broker.receive(peer,op.channel,op.after,op.timeout)
        elif op.action == "finish":
            return await broker.finish(peer,op.channel)
        elif op.action == "result":
            return broker.delegation_result(peer, op.channel)
        elif op.action == "round_result":
            return broker.round_result(peer, op.channel, op.question_id)
        return broker.snapshot(peer)

    async def invoke(ctx, **payload):
        peer = ctx.request_context.request.state.peer
        try:
            return await execute(peer, operation(payload))
        except ConnectorError as exc:
            broker.record_incident(peer, exc.code, str(exc))
            raise

    # Message state belongs to the broker, so HTTP reconnects need no MCP session affinity.
    mcp_app = create_mcp(invoke).streamable_http_app(stateless_http=True, json_response=True, max_request_body_size=100000)
    mcp_app.add_middleware(MCPAuthMiddleware, authenticate=auth, generic_peers=generic_peers)

    async def call(req: Request):
        peer = auth(req)
        req.state.peer=peer
        op = operation(await body(req))
        if op.action == "receive":
            async def disconnected():
                while (await req.receive())["type"] != "http.disconnect":
                    pass
            waiting=asyncio.create_task(execute(peer,op))
            disconnect=asyncio.create_task(disconnected())
            try:
                done,_=await asyncio.wait([waiting,disconnect],return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done:
                    await disconnect
                    return Response(status_code=499)
                result=await waiting
            finally:
                for task in (waiting,disconnect):
                    if not task.done():task.cancel()
                for task in (waiting,disconnect):
                    try:await task
                    except asyncio.CancelledError:pass
        else:
            result = await execute(peer,op)
        return JSONResponse(result)

    async def admin(req: Request):
        auth(req,admin=True)
        if req.method == "GET":
            return JSONResponse(broker.snapshot())
        op = await body(req)
        if not isinstance(op,dict) or set(op)!={"action","channel"} or op["action"] not in ("approve","deny","revoke") or not isinstance(op["channel"],str):
            raise ConnectorError("invalid_request", "管理操作参数无效")
        if op["action"] == "revoke":
            result = await broker.revoke(op["channel"])
        else:
            result = await broker.decide(op["channel"],op["action"]=="approve")
        return JSONResponse(result)

    async def decision(req: Request):
        peer = auth(req, approval=True)
        req.state.peer = peer
        op = await body(req)
        if (not isinstance(op, dict) or set(op) not in ({"channel", "decision"}, {"channel", "decision", "source"}) or
                not isinstance(op["channel"], str) or
                op["decision"] not in ("accept", "decline", "cancel") or
                op.get("source", "host_elicitation") not in ("host_elicitation", "host_tool_permission")):
            raise ConnectorError("invalid_request", "审批需提供 channel 和 accept、decline 或 cancel 决定")
        result = await broker.decide(op["channel"], op["decision"] == "accept",
                                     host_approval=(peer, op["decision"]),
                                     host_approval_source=op.get("source", "host_elicitation"))
        return JSONResponse(result)

    async def error(req, exc):
        if getattr(req.state,"peer",None):
            broker.record_incident(req.state.peer,exc.code,str(exc))
        code = 401 if exc.code=="unauthorized" else 403 if exc.code=="forbidden" else 409
        return JSONResponse({"error":exc.code,"message":str(exc)},status_code=code)

    app = Starlette(routes=[Route("/call",call,methods=["POST"]),Route("/admin",admin,methods=["GET","POST"]),Route("/decision",decision,methods=["POST"]),Mount("/",app=mcp_app)],exception_handlers={ConnectorError:error},lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=["127.0.0.1","localhost","testserver"])
    app.state.broker = broker
    return app
