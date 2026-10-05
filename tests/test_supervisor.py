import httpx
import pytest

from local_ai_connector import supervisor
from local_ai_connector.core import ConnectorError


@pytest.mark.parametrize("response,code",[
    (httpx.Response(401,text="echoed-secret"),"model_http_error"),
    (httpx.Response(200,json={"choices":[]}),"invalid_model_response"),
    (httpx.Response(200,json={"choices":[{"message":{"content":'{"explanation":"执行操作","suggestion":"approve"}'}}]}),"invalid_model_response"),
])
async def test_model_errors_do_not_expose_upstream_content(monkeypatch,response,code):
    client=httpx.AsyncClient
    monkeypatch.setattr(supervisor.httpx,"AsyncClient",lambda **kwargs:client(transport=httpx.MockTransport(lambda request:response),**kwargs))
    with pytest.raises(ConnectorError) as error:
        await supervisor.advise({"base_url":"https://example.com/v1","model":"test","api_key":"echoed-secret"},{"code":"file_busy"})
    assert error.value.code==code
    assert "echoed-secret" not in str(error.value)
