import asyncio
import json
from pathlib import Path
import httpx

from .core import ConnectorError


class Client:
    def __init__(self, config_file: Path, *, restart=None):
        config = json.loads(config_file.read_text())
        self.config = config
        self.http = httpx.AsyncClient(base_url=config["url"],headers={"Authorization":"Bearer "+config["token"]},trust_env=False)
        # Optional: restart this installation's service when nothing is listening.
        self.restart = restart

    async def _post(self, path, **kwargs):
        try:
            return await self.http.post(path, **kwargs)
        except httpx.ConnectError:
            if self.restart is None:
                raise
            # A refused connection delivered nothing, so one retry cannot duplicate a request.
            await asyncio.to_thread(self.restart)
            return await self.http.post(path, **kwargs)

    async def call(self, **payload):
        response = await self._post("/call",json=payload,timeout=payload.get("timeout",30)+10)
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
        response = await self._post("/decision", json=payload,
                                    headers={"Authorization": "Bearer " + token}, timeout=30)
        result = response.json()
        if response.is_error:
            raise ConnectorError(result.get("error", "approval_error"), result.get("message", "提交用户决定失败"))
        return result

    async def decide_binding(self, request_id, decision, session, *, source):
        token = self.config.get("approval_token")
        if not isinstance(token, str) or not token:
            raise ConnectorError("approval_not_configured", "This endpoint has no trusted host approval credential")
        response = await self._post("/decision", json={"binding_request": request_id, "decision": decision,
                                                       "source": source, "session": session},
                                    headers={"Authorization": "Bearer " + token}, timeout=30)
        result = response.json()
        if response.is_error:
            raise ConnectorError(result.get("error", "approval_error"), result.get("message", "Binding decision failed"))
        return result

    async def close(self):
        await self.http.aclose()
