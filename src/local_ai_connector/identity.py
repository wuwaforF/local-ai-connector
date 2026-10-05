import fcntl
import json
import os
from pathlib import Path
import tempfile

from .core import ConnectorError, require


LEGACY_IDENTITIES = {
    "codex": {"namespace": "codex", "path": ["x-codex-turn-metadata", "thread_id"]},
    "zcode": {"namespace": "zcode", "path": ["com.zcode/request-context", "session_id"]},
}


def validate_identity(identity):
    require(isinstance(identity, dict) and set(identity) == {"namespace", "path"},
            "invalid_identity_config", "身份映射需要 namespace 和 path")
    namespace, path = identity["namespace"], identity["path"]
    require(isinstance(namespace, str) and 0 < len(namespace) <= 100 and ":" not in namespace,
            "invalid_identity_config", "身份命名空间无效")
    require(isinstance(path, list) and 0 < len(path) <= 10 and
            all(isinstance(key, str) and 0 < len(key) <= 200 for key in path),
            "invalid_identity_config", "身份路径须为非空字段名称列表")


def caller_session(client: str, meta: dict, identity: dict | None = None):
    if client=="generic":
        # Generic clients authenticate an endpoint, not a host's conversation.
        return None
    if client == "metadata":
        validate_identity(identity)
    else:
        identity = LEGACY_IDENTITIES.get(client)
        require(identity is not None, "unsupported_client", "端点客户端类型不受支持；请选择 generic 或配置 metadata 身份映射")
    session = meta
    for key in identity["path"]:
        session = session.get(key) if isinstance(session, dict) else None
    require(isinstance(session,str) and 0<len(session)<=200,"missing_session_identity","宿主未提供原生任务身份，无法安全绑定此端点")
    return identity["namespace"]+":"+session


def bind_session(config_path: Path, session: str):
    """Pin a peer to its first native caller; models cannot choose this tool-external identity."""
    with config_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        config=json.loads(config_path.read_text())
        current=config.get("bound_session")
        require(current is None or current==session,"session_conflict","此端点已绑定另一桌面任务。请为新的任务对创建独立连接器配置。")
        if current is None:
            config["bound_session"]=session
            fd,temp=tempfile.mkstemp(dir=config_path.parent,prefix="peer-",suffix=".tmp")
            try:
                with os.fdopen(fd,"w") as file:
                    json.dump(config,file,ensure_ascii=False,indent=2)
                os.replace(temp,config_path)
            finally:
                if os.path.exists(temp):os.unlink(temp)
        return config
