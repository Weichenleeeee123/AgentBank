from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app
from agentbank.auth.devices import DeviceService


def test_generic_red_capability_api_can_finish_with_device_otp(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'red-api.db'}")
    device_token = DeviceService(app.state.database).register("user_001")
    client = TestClient(app)
    assert client.get("/device/v1/inbox", headers={"X-Device-Token": device_token}).status_code == 200
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/v1/risk/submit", headers=headers, json={"answers": [2] * 10})
    pending = client.post("/v1/capabilities/W04", headers=headers, json={
        "capability_id": "W04", "idempotency_key": "api-red-w04", "payload": {
            "product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa",
        },
    })
    assert pending.status_code == 200
    record_id = pending.json()["record_id"]
    from agentbank.auth.devices import DeviceService as Device
    code = Device(app.state.database).issue_otp("user_001", record_id)
    completed = client.post(f"/v1/capability-records/{record_id}/verify", headers=headers, json={"otp": code})
    assert completed.status_code == 200
    assert completed.json()["status"] == "succeeded"
