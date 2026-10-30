from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_pwa_shell_is_served_by_app(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'pwa.db'}"))
    response = client.get("/app/")
    assert response.status_code == 200
    assert "AgentBank" in response.text
    assert client.get("/app/app.js").status_code == 200
    source = client.get("/app/app.js").text
    assert "window.prompt" not in source and "prompt(" not in source
    assert client.get("/app/index.html").text.find('id="otpDialog"') >= 0
    for control in ("userSelect", "notificationList", "pinDialog", "record_id"):
        assert control in client.get("/app/index.html").text or control in source


def test_pwa_exposes_direct_balance_transaction_and_transfer_forms(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'pwa-forms.db'}"))
    html = client.get("/app/").text
    source = client.get("/app/app.js").text

    for control in ("queryForm", "transferForm", "queryMode", "transferMode",
                    "startDate", "endDate", "categoryFilter", "transactionLimit", "recipientName",
                    "recipientPhone", "recipientAlias", "transferAmount", "transferMemo"):
        assert f'id="{control}"' in html
    assert 'id="queryMode"' in html and "余额" in html and "交易" in html
    assert 'id="transferMode"' in html and "姓名" in html and "手机号" in html and "别名" in html
    assert 'type="password"' in html and 'id="otpDialog"' in html
    assert '/v1/forms/${path}' in source
    assert '/v1/capability-records/${recordId}/confirm' in source
    assert '/v1/capability-records/${recordId}/verify' in source
    assert "amount_minor" in source and "start_date" in source and "end_date" in source
    assert "direct_form: true" in source
    assert "Object.entries(payload)" not in source


def test_pwa_form_api_journeys_share_capability_prepare_and_confirmation(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'pwa-form-journeys.db'}"))
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    balance = client.post("/v1/forms/balance", headers=headers, json={}).json()
    transactions = client.post("/v1/forms/transactions", headers=headers,
                               json={"limit": 5, "category": "餐饮"}).json()
    assert balance["capability_id"] == "Q01" and balance["status"] == "succeeded"
    assert transactions["capability_id"] == "Q02" and transactions["status"] == "succeeded"
    assert len(transactions["data"]["transactions"]) <= 5
    assert all(row["category"] == "餐饮" for row in transactions["data"]["transactions"])

    transfers = [
        ("transfer", {"recipient_name": "房东", "amount_minor": 1250}),
        ("transfer-phone", {"phone": "13800000001", "amount_minor": 1250}),
        ("transfer-alias", {"alias": "房东", "amount_minor": 1250}),
    ]
    for kind, payload in transfers:
        prepared = client.post(f"/v1/forms/{kind}", headers=headers, json=payload).json()
        assert prepared["status"] == "awaiting_confirmation"
        assert prepared["data"]["display"]
        confirmed = client.post(f"/v1/capability-records/{prepared['record_id']}/confirm", headers=headers,
                               json={"receipt_id": prepared["data"]["receipt_id"],
                                     "display_hash": prepared["data"]["display_hash"]}).json()
        assert confirmed["status"] == "succeeded"
