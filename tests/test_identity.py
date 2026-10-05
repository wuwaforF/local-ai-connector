import json
import pytest
from local_ai_connector.core import ConnectorError
from local_ai_connector.identity import caller_session,bind_session


def test_native_metadata_and_session_binding(tmp_path):
    assert caller_session("codex",{"x-codex-turn-metadata":{"thread_id":"task-a"}})=="codex:task-a"
    assert caller_session("zcode",{"com.zcode/request-context":{"session_id":"task-b"}})=="zcode:task-b"
    with pytest.raises(ConnectorError):caller_session("codex",{})
    with pytest.raises(ConnectorError):caller_session("zcode",{"session_id":"wrong-place"})
    path=tmp_path/"peer.json"
    path.write_text(json.dumps({"peer":"test"}))
    bind_session(path,"codex:task-a")
    bind_session(path,"codex:task-a")
    with pytest.raises(ConnectorError,match="另一桌面任务"):bind_session(path,"codex:task-b")
    assert json.loads(path.read_text())["bound_session"]=="codex:task-a"


def test_generic_identity_is_endpoint_scoped():
    assert caller_session("generic", {}) is None
    assert caller_session("generic", {"x-codex-turn-metadata": {"thread_id": "other"}}) is None
    with pytest.raises(ConnectorError, match="不受支持"):
        caller_session("unknown", {})
