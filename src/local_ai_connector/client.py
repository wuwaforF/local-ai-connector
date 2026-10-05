import json
from pathlib import Path
import httpx

from .core import ConnectorError


class Client:
    def __init__(self, config_file: Path):
        config = json.loads(config_file.read_text())
        self.config = config
        self.http = httpx.AsyncClient(base_url=config["url"],headers={"Authorization":"Bearer "+config["token"]},trust_env=False)

    async def call(self, **payload):
        response = await self.http.post("/call",json=payload,timeout=payload.get("timeout",30)+10)
        result = response.json()
        if response.is_error:
            raise ConnectorError(result.get("error","transport_error"),result.get("message","连接器请求失败"))
        return result

    async def decide(self, channel, decision, *, source="host_elicitation"):
        token = self.config.get("approval_token")
        if not isinstance(token, str) or not token:
            raise ConnectorError("approval_not_configured", "此端点未配置可信宿主审批")
        payload = {"channel": channel, "decision": decision}
        if source != "host_elicitation":
            payload["source"] = source
        response = await self.http.post("/decision", json=payload,
                                        headers={"Authorization": "Bearer " + token}, timeout=30)
        result = response.json()
        if response.is_error:
            raise ConnectorError(result.get("error", "approval_error"), result.get("message", "提交用户决定失败"))
        return result

    async def close(self):
        await self.http.aclose()
