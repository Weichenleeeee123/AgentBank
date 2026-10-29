import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def signed(client: TestClient, app, payload: dict[str, object]):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    signature = hmac.new(app.state.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return client.post("/v1/im/webim/events", json=payload, headers={"X-IM-Signature": signature})


def test_webim_red_flow_exposes_nonce_deep_link_and_consumes_callback(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'webim-callback.db'}")
    client = TestClient(app)
    payload = {"event_id": "webim-red", "user_id": "user_001", "text": "给房东转1200元"}
    response = signed(client, app, payload)
    assert response.status_code == 200
    assert "app_deep_link" in response.json()
    nonce = response.json()["app_deep_link"].split("nonce=", 1)[1]
    callback = signed(client, app, {"event_id": "callback-1", "event_type": "card_callback", "user_id": "user_001", "nonce": nonce})
    assert callback.json()["type"] == "callback_ack"
    assert callback.json()["record_id"]


def test_webim_group_hides_private_result(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'webim-group.db'}")
    client = TestClient(app)
    response = signed(client, app, {"event_id": "webim-group", "user_id": "user_001", "text": "查余额", "group": True})
    assert response.json()["type"] == "generic_status"


def test_webim_callback_can_execute_confirmed_record_and_returns_result(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'webim-execute.db'}")
    client = TestClient(app)
    response = signed(client, app, {"event_id": "webim-confirm", "user_id": "user_001", "text": "给房东转20元"})
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "confirmation"
    callback = signed(client, app, {"event_id": "callback-confirm", "event_type": "card_callback",
                                    "user_id": "user_001", "nonce": body["app_deep_link"].split("nonce=", 1)[1],
                                    "action": "confirm", "display_hash": body["display_hash"],
                                    "receipt_id": body["data"]["receipt_id"]})
    assert callback.json()["type"] == "result"
    assert callback.json()["data"]["transfer_result"]["status"] == "succeeded"
