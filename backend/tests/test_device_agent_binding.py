from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app
from agentbank.auth.devices import DeviceService


def test_device_inbox_binds_token_for_agent_red_challenge(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'device-bind.db'}")
    token = DeviceService(app.state.database).register("user_001")
    client = TestClient(app)
    response = client.get("/device/v1/inbox", headers={"X-Device-Token": token})
    assert response.status_code == 200
    assert app.state.capabilities.device_tokens["user_001"] == token
