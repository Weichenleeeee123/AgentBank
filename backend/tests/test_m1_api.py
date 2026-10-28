from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_api_exposes_session_balance_and_transfer_flow(tmp_path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'api.db'}")
    client = TestClient(app)

    session = client.post("/v1/sessions", json={"user_id": "user_001"})
    assert session.status_code == 200
    token = session.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    balance = client.get("/v1/accounts/balance", headers=headers)
    assert balance.status_code == 200
    assert balance.json()["available_minor"] == 500_000

    prepared = client.post(
        "/v1/transfers/prepare",
        headers=headers,
        json={"recipient_name": "房东", "amount_minor": 20_000},
    )
    assert prepared.status_code == 200
    data = prepared.json()
    confirmed = client.post(
        f"/v1/plans/{data['plan_id']}/confirm",
        headers=headers,
        json={"receipt_id": data["receipt_id"], "display_hash": data["display_hash"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "succeeded"
