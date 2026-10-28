import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_m2_api_exposes_analysis_subscription_card_and_plan_views(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'm2-api.db'}")
    client = TestClient(app)
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    transactions = client.get("/v1/transactions", headers=headers)
    assert transactions.status_code == 200
    assert transactions.json()["transactions"]

    report = client.post("/v1/analysis/run", headers=headers, json={"source": "transactions", "group_by": ["month"], "limit": 10})
    assert report.status_code == 200
    assert report.json()["query_hash"].startswith("sha256:")

    subscriptions = client.get("/v1/subscriptions", headers=headers)
    assert subscriptions.status_code == 200
    assert subscriptions.json()["subscriptions"]

    prepared = client.post("/v1/transfers/prepare", headers=headers, json={"recipient_name": "房东", "amount_minor": 1_000})
    plan = client.get(f"/v1/plans/{prepared.json()['plan_id']}", headers=headers)
    assert plan.status_code == 200
    assert plan.json()["nodes"][0]["tool"] == "transfer.create"


def test_webim_signature_and_replay_protection(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'webim.db'}")
    client = TestClient(app)
    event = {"event_id": "evt_1", "user_id": "user_001", "text": "查余额"}
    secret = app.state.session_secret
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    response = client.post("/v1/im/webim/events", json=event, headers={"X-IM-Signature": signature})
    assert response.status_code == 200
    assert response.json()["type"] == "facts"
    replay = client.post("/v1/im/webim/events", json=event, headers={"X-IM-Signature": signature})
    assert replay.status_code == 409


def test_conversation_api_runs_agent_transfer_flow(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'conversation.db'}")
    client = TestClient(app)
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/v1/conversations/c1/messages", headers=headers,
                           json={"text": "给房东转 20 元，备注房租"})
    assert response.status_code == 200
    assert response.json()["kind"] == "confirmation"
    confirmed = client.post(f"/v1/conversations/c1/records/{response.json()['record_id']}/confirm", headers=headers,
                            json={"receipt_id": response.json()["data"]["receipt_id"],
                                  "display_hash": response.json()["display_hash"]})
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["transfer_result"]["status"] == "succeeded"
