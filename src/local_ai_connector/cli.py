import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

from .registry import MCP_LOCALES


def save_private(path: Path, value):
    from . import os_adapter
    if not path.parent.exists():
        os_adapter.ensure_private_dir(path.parent)
    os_adapter.create_private_file(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def peer_name(value):
    if not 1 <= len(value) <= 100 or not value.replace("-", "").replace("_", "").isalnum() or value in {"server", "model"}:
        raise argparse.ArgumentTypeError("端点名称须为 1 至 100 个字母、数字、连字符或下划线，且不能为 server 或 model")
    return value


def main():
    if sys.platform == "win32":
        # Redirected Windows output defaults to the ANSI code page, which cannot encode every
        # path or message; a detached service writing to its log would otherwise fail at start.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="本地 AI 连接器")
    parser.add_argument("--data",type=Path,default=Path(".connector"))
    sub = parser.add_subparsers(dest="command",required=True)
    setup = sub.add_parser("init",help="创建独立数据目录及多个通信端点")
    setup.add_argument("--port",type=int,default=38471)
    setup.add_argument("--peers",nargs="+",type=peer_name,default=["worker-a","worker-b"])
    setup.add_argument("--native-peer",action="append",default=[],metavar="PEER=CLIENT",help="可选原生任务绑定，CLIENT 为 codex 或 zcode；其余端点使用 generic")
    register = sub.add_parser("peer-add", help="服务停止时登记新工作者，重启后可通过 MCP 发现")
    register.add_argument("peer", type=peer_name)
    register.add_argument("--name", default="")
    register.add_argument("--description", default="")
    register.add_argument("--capability", action="append", default=[])
    register.add_argument("--session-namespace")
    register.add_argument("--session-path", nargs="+")
    register.add_argument("--mcp-locale", choices=MCP_LOCALES, default="zh-CN", help="此端点的 stdio MCP 指令和工具说明语言")
    chat_approval = sub.add_parser("enable-chat-approval", help="服务停止时为指定工作者启用聊天内委派确认")
    chat_approval.add_argument("peers", nargs="+", type=peer_name)
    chat_approval.add_argument("--mcp-locale", choices=MCP_LOCALES, default="en-US")
    sub.add_parser("serve",help="启动本机连接服务")
    sub.add_parser("service-config", help="导出独立运行服务的 macOS launchd 配置，不自动安装")
    mcp = sub.add_parser("mcp",help="供 MCP 客户端启动的 stdio 入口")
    mcp.add_argument("--config",type=Path,required=True)
    mcp.add_argument("--claude-binding-root", type=Path,
                     help="启用本机聊天内 Claude 会话只读核对；不登记或修改绑定")
    mcp.add_argument('--codex-quick-binding', action='store_true',
                     help='启用 Codex 保存协作目标及按任务快速绑定候选；服务须另行配置元数据接口')
    mcp.add_argument("--start-service", action="store_true",
                     help="start this installation's service if it is not running")
    install = sub.add_parser("setup", help="install or update the connector for one desktop host (idempotent)")
    install.add_argument("host", choices=["codex", "claude", "antigravity"])
    install.add_argument("--profile", default="default", help="separate installation name (default: default)")
    install.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    install.add_argument("--no-start", action="store_true", help="do not start the service now")
    waking = install.add_mutually_exclusive_group()
    waking.add_argument("--wake", dest="wake", action="store_const", const=True, default=None,
                        help="wake the bound chat automatically when a task is approved (Codex, macOS; opt-in experiment)")
    waking.add_argument("--no-wake", dest="wake", action="store_const", const=False,
                        help="turn automatic wake-up off again")
    remove = sub.add_parser("uninstall", help="remove host entries this installation created")
    remove.add_argument("--profile", default="default")
    remove.add_argument("--host", action="append", choices=["codex", "claude", "antigravity"])
    remove.add_argument("--purge", action="store_true", help="also delete this installation's data directory")
    remove.add_argument("--dry-run", action="store_true")
    check = sub.add_parser("doctor", help="check readiness of an installation")
    check.add_argument("--profile", default="default")
    check.add_argument("--host", choices=["codex", "claude", "antigravity"])
    unbind = sub.add_parser("unbind", help="remove the bound chat of a worker endpoint (owner)")
    unbind.add_argument("endpoint", type=peer_name)
    admin = sub.add_parser("approve",help="显示原始求助并批准或拒绝")
    admin.add_argument("channel",nargs="?")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("channel")
    sub.add_parser("status")
    sub.add_parser("wakeup-status",help="查看可选唤醒适配器的派发记录（只读）")
    config = sub.add_parser("client-config",help="生成客户端 MCP 配置片段")
    config.add_argument("--peer",type=peer_name,required=True)
    config.add_argument("--client",choices=["generic","codex","zcode"],default="generic")
    config.add_argument("--transport",choices=["stdio","streamable-http"],default="stdio")
    call = sub.add_parser("call",help="通过同一接口诊断连接")
    call.add_argument("--peer",type=peer_name,required=True)
    call.add_argument("payload",help="JSON 操作参数")
    sub.add_parser("model-config",help="配置可替换的监管模型")
    sub.add_parser("model-test",help="使用中文异常样本测试模型连接")
    explain=sub.add_parser("model-explain",help="解释已记录的真实连接异常")
    explain.add_argument("incident")
    ui = sub.add_parser("ui",help="打开本地批准与模型配置窗口")
    read = sub.add_parser("file-version",help="读取受控文件的版本摘要")
    read.add_argument("--root",type=Path,required=True)
    read.add_argument("path")
    write = sub.add_parser("file-write",help="经占用和版本检查写入文件")
    write.add_argument("--root",type=Path,required=True)
    write.add_argument("--source",type=Path,required=True)
    write.add_argument("--expected",required=True,help="file-version 返回的摘要；新文件填 missing")
    write.add_argument("path")
    args = parser.parse_args()
    data = args.data.resolve()
    if args.command in ("setup", "uninstall", "doctor"):
        from .hosts import HostConfigError
        from .install import InstallError, doctor, setup, uninstall
        from .service import ServiceError
        try:
            if args.command == "setup":
                report = setup(args.host, profile=args.profile, dry_run=args.dry_run, start_service=not args.no_start,
                               wake=args.wake)
            elif args.command == "uninstall":
                report = uninstall(profile=args.profile, hosts=args.host, purge=args.purge, dry_run=args.dry_run)
            else:
                report = doctor(profile=args.profile, host=args.host)
        except (InstallError, HostConfigError, ServiceError) as exc:
            print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(1)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.command == "doctor" and not report["ready"]:
            sys.exit(1)
        return
    if args.command == "init":
        if not 1024<=args.port<=65535 or len(args.peers)<2 or len(set(args.peers))!=len(args.peers):
            parser.error("端口或端点名称无效")
        native = {}
        for item in args.native_peer:
            peer, sep, client = item.partition("=")
            if not sep or peer not in args.peers or peer in native or client not in ("codex", "zcode"):
                parser.error("--native-peer 须为已登记且不重复的 PEER=codex 或 PEER=zcode")
            native[peer] = client
        if any((data/f"{name}.json").exists() for name in ["server", *args.peers]):
            parser.error("目标配置已存在，请使用新的数据目录")
        config = {"url":f"http://127.0.0.1:{args.port}","port":args.port,"admin_token":secrets.token_urlsafe(32),"peers":{p:secrets.token_urlsafe(32) for p in args.peers}}
        save_private(data/"server.json",config)
        for peer,token in config["peers"].items():
            save_private(data/f"{peer}.json",{"url":config["url"],"token":token,"peer":peer,"client":native.get(peer,"generic")})
        print(f"已初始化：{data}\n端点：{'、'.join(args.peers)}\n使用 serve 启动。")
    elif args.command == "peer-add":
        from .registry import add_peer
        if bool(args.session_namespace) != bool(args.session_path):
            parser.error("--session-namespace 和 --session-path 须一起指定")
        identity = {"namespace": args.session_namespace, "path": args.session_path} if args.session_path else None
        try:
            path = add_peer(data, args.peer, {"name": args.name, "description": args.description,
                                            "capabilities": args.capability}, identity, mcp_locale=args.mcp_locale)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"已登记：{args.peer}；私有配置：{path}。重启服务后生效。")
    elif args.command == "enable-chat-approval":
        from .registry import enable_chat_approval
        try:
            enable_chat_approval(data, args.peers, mcp_locale=args.mcp_locale)
        except ValueError as exc:
            parser.error(str(exc))
        print("已启用聊天内确认：" + "、".join(args.peers) + "。重启服务并重新加载宿主 MCP 后生效。")
    elif args.command == "service-config":
        import plistlib
        config = {"Label": "dev.local-ai-connector.service",
                  "ProgramArguments": [sys.executable, "-m", "local_ai_connector.cli", "--data", str(data), "serve"],
                  "EnvironmentVariables": {"PYTHONPATH": str(Path(__file__).resolve().parent.parent),
                                           "PYTHONDONTWRITEBYTECODE": "1"},
                  "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
                  "StandardOutPath": str(data / "service.log"), "StandardErrorPath": str(data / "service-error.log")}
        print(plistlib.dumps(config).decode(), end="")
    elif args.command == "serve":
        import uvicorn
        from . import os_adapter
        from .server import create_app
        from contextlib import ExitStack
        # Serialize service ownership before opening SQLite; never split waiters across processes.
        import time
        with ExitStack() as owned:
            # Windows releases a crashed owner's lock asynchronously, so wait briefly before giving up.
            deadline = time.monotonic() + 5
            while True:
                try:
                    owned.enter_context(os_adapter.exclusive_lock(data/"server.lock"))
                    break
                except os_adapter.LockBusy:
                    if time.monotonic() >= deadline:
                        parser.error("another connector service already owns this data directory")
                    time.sleep(0.2)
            os.umask(0o077)
            config=json.loads((data/"server.json").read_text())
            print(f"Local AI Connector listening on {config['url']} (Ctrl-C to stop)", flush=True)
            app = create_app(data)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config["port"], access_log=False,
                                                   timeout_graceful_shutdown=5))
            # A portable owner-initiated stop (POST /admin shutdown); signals differ across platforms.
            app.state.request_exit = lambda: setattr(server, "should_exit", True)
            server.run()
    elif args.command == "mcp":
        from .mcp_server import serve
        if args.claude_binding_root is not None and args.config.is_symlink():
            parser.error("会话核对须使用当前用户的普通端点配置文件")
        if args.start_service:
            from .service import ServiceError, ensure_for_endpoint
            try:
                ensure_for_endpoint(args.config.resolve())
            except ServiceError as exc:
                # Keep serving MCP: tool calls then report the unavailable service to the chat.
                print(f"local-ai-connector: {exc.code}: {exc}", file=sys.stderr, flush=True)
        serve(args.config.resolve(), claude_binding_root=args.claude_binding_root,
              codex_quick_binding=args.codex_quick_binding, start_service=args.start_service)
    elif args.command == "wakeup-status":
        if not (data/"wakeup.sqlite3").is_file():
            parser.error("未找到唤醒派发记录；唤醒适配器默认关闭")
        from .wakeup import WakeStore
        store=WakeStore(data/"wakeup.sqlite3")
        try:
            print(json.dumps(store.report(),ensure_ascii=False,indent=2))
        finally:
            store.close()
    elif args.command == "file-version":
        from .files import snapshot
        print(snapshot(args.root,args.path) or "missing")
    elif args.command == "file-write":
        from .files import write_file
        print(write_file(args.root,args.path,args.source.read_bytes(),None if args.expected=="missing" else args.expected))
    elif args.command == "ui":
        import subprocess
        import tempfile
        if sys.platform != "darwin":
            parser.error("首版图形入口支持 macOS；其他系统请使用 approve 和 model-config")
        import plistlib
        source=Path(__file__).with_name("ui.swift")
        bundle=data/"Local AI Connector.app"/"Contents"
        binary=bundle/"MacOS"/"connector-ui"
        binary.parent.mkdir(parents=True,exist_ok=True)
        (bundle/"Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier":"dev.local-ai-connector.control","CFBundleName":"本地 AI 连接器","CFBundleExecutable":"connector-ui","CFBundlePackageType":"APPL","NSHighResolutionCapable":True,"ConnectorPython":sys.executable}))
        if not binary.exists() or binary.stat().st_mtime<source.stat().st_mtime:
            with tempfile.TemporaryDirectory(prefix="connector-swift-") as cache:
                subprocess.run(["xcrun","swiftc","-module-cache-path",cache,str(source),"-o",str(binary)],check=True)
        subprocess.run([str(binary),str(data),sys.executable],check=True)
    elif args.command == "client-config":
        config_path = data/f"{args.peer}.json"
        if not config_path.is_file():
            parser.error("端点不存在")
        cmd = ["-m","local_ai_connector.cli","mcp","--config",str(config_path)]
        if args.transport == "streamable-http":
            peer_config = json.loads(config_path.read_text())
            if peer_config.get("client", "generic") != "generic":
                parser.error("原生任务绑定端点仅使用 stdio；HTTP 接入请创建独立 generic 端点")
            if args.client == "zcode":
                parser.error("HTTP 配置请使用 --client generic，再按宿主格式填写 URL 和 headers")
            entry = {"url": peer_config["url"].rstrip("/")+"/mcp", "headers": {"Authorization": "Bearer "+peer_config["token"]}}
        else:
            entry = {"command": sys.executable, "args": cmd}
        if args.client == "codex":
            print('[mcp_servers.local_ai_connector]')
            for key, value in entry.items():
                if key != "headers":
                    print(key+' = '+json.dumps(value))
            print('startup_timeout_sec = 20\ntool_timeout_sec = 60\nrequired = true')
            if "headers" in entry:
                print('[mcp_servers.local_ai_connector.http_headers]\nAuthorization = '+json.dumps(entry["headers"]["Authorization"]))
        elif args.client == "zcode":
            print(json.dumps({"mcp":{"servers":{"local_ai_connector":{**entry,"enable":True,"timeoutMs":60000}}}},ensure_ascii=False,indent=2))
        else:
            print(json.dumps({"mcpServers":{"local_ai_connector":entry}},ensure_ascii=False,indent=2))
    elif args.command == "call":
        from .client import Client
        async def call():
            client=Client(data/f"{args.peer}.json")
            try:
                return await client.call(**json.loads(args.payload))
            finally:
                await client.close()
        print(json.dumps(asyncio.run(call()),ensure_ascii=False,indent=2))
    elif args.command in ("model-config","model-test","model-explain"):
        from .supervisor import configure, test_model, advise
        if args.command=="model-config":
            configure(data)
        elif args.command=="model-test":
            print(json.dumps(asyncio.run(test_model(data)),ensure_ascii=False,indent=2))
        else:
            import httpx
            config=json.loads((data/"server.json").read_text())
            with httpx.Client(trust_env=False,timeout=5) as http:
                r=http.get(config["url"]+"/admin",headers={"Authorization":"Bearer "+config["admin_token"]})
                r.raise_for_status()
            incidents=[i for i in r.json()["incidents"] if i["id"]==args.incident]
            if not incidents:parser.error("找不到该异常记录")
            model_config=json.loads((data/"model.json").read_text())
            i=incidents[0]
            print(json.dumps(asyncio.run(advise(model_config,{"code":i["code"],"facts":i["detail"]})),ensure_ascii=False,indent=2))
    else:
        import httpx
        config=json.loads((data/"server.json").read_text())
        with httpx.Client(base_url=config["url"],headers={"Authorization":"Bearer "+config["admin_token"]},trust_env=False,timeout=10) as http:
            if args.command == "unbind":
                r=http.post("/admin",json={"action":"unbind","endpoint":args.endpoint})
                if r.is_error:
                    print(r.text, file=sys.stderr)
                    sys.exit(1)
                print(json.dumps(r.json(),ensure_ascii=False,indent=2))
                return
            if args.command == "revoke":
                r=http.post("/admin",json={"action":"revoke","channel":args.channel})
                r.raise_for_status()
                print("连接已撤销")
                return
            r=http.get("/admin")
            r.raise_for_status()
            channels=r.json()["channels"]
            if args.command=="status":
                print(json.dumps(r.json(),ensure_ascii=False,indent=2))
                return
            pending=[c for c in channels if c["status"]=="pending" and (not args.channel or c["id"]==args.channel)]
            if not pending:
                print("没有待批准求助")
            for c in pending:
                print(f"\n{c['requester']} → {c['responder']}\n通道：{c['id']}\n原始求助：\n{c['original']}\n")
                selected = c.get('conversation', {}).get('selected')
                if selected is not None:
                    print(f"选定 Codex 聊天：{selected['title']}\n聊天 ID：{selected['thread_id']}\n工作区：{selected['workspace']}\n")
                prompt = ("仅保存此发起端点的 Codex 目标；不发送任务，后续新任务仍须批准。输入 yes 保存，其他内容拒绝："
                          if c.get('conversation', {}).get('binding_only') else
                          "批准本次双向交流？输入 yes 批准，其他内容拒绝：")
                approve=input(prompt).strip()=="yes"
                r=http.post("/admin",json={"action":"approve" if approve else "deny","channel":c["id"]})
                r.raise_for_status()
                print("已批准" if approve else "已拒绝")


if __name__=="__main__":
    main()
