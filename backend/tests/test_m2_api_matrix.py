from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentbank.api.app import create_app


CAPABILITIES = [
    "Q01", "Q02", "T01", "T02", "T03", "T04", "T05", "B01", "B02", "B03", "B04",
    "W01", "W02", "W03", "W04", "W05", "W06", "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08",
    "S01", "S02", "S03", "X01", "X02", "X03", "X04", "X05",
]


def payload_for(capability_id: str) -> dict[str, object]:
    if capability_id == "Q02":
        return {"limit": 5}
    if capability_id in {"T01", "T04"}:
        return {"recipient_name": "房东", "amount_minor": 1_000}
    if capability_id == "T02":
        return {"phone": "13800000001", "amount_minor": 1_000}
    if capability_id == "T03":
        return {"alias": "房东", "amount_minor": 1_000}
    if capability_id == "T05":
        return {"total_minor": 10_000, "participant_ids": ["user_001", "user_002", "user_003"]}
    if capability_id == "B01":
        return {"dsl": {"source": "transactions", "group_by": ["month"], "limit": 10}}
    if capability_id == "B03":
        return {"start_month": "2026-05", "end_month": "2026-07"}
    if capability_id == "B04":
        return {"start_month": "2026-01", "end_month": "2026-12"}
    if capability_id == "W03":
        return {"answers": [1] * 10}
    if capability_id == "W04":
        return {"product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa"}
    if capability_id == "W05":
        return {"assurance": "mfa"}
    if capability_id == "C01":
        return {"virtual": True}
    if capability_id == "C02":
        return {"card_id": "card_001", "limit_minor": 90_000}
    if capability_id in {"C03", "C04"}:
        return {"card_id": "card_001", "restriction": "online", **({"assurance": "mfa"} if capability_id == "C04" else {})}
    if capability_id in {"C05", "C06", "C07", "C08"}:
        return {"card_id": "card_001", "assurance": "mfa", **({"new_pin": "1234"} if capability_id == "C08" else {})}
    if capability_id == "S02":
        return {"subscription_id": "sub_video", "days": 3}
    if capability_id == "S03":
        return {"subscription_id": "sub_video"}
    if capability_id == "X02":
        return {"amount_minor": 1_000}
    if capability_id in {"X03", "X04"}:
        return {"delivery_date": "2026-11-13"}
    if capability_id == "X05":
        return {"card_id": "card_001"}
    return {}


def execute_capability(client: TestClient, headers: dict[str, str], device_token: str,
                       capability_id: str, payload: dict[str, object], key: str) -> dict[str, object]:
    response = client.post(f"/v1/capabilities/{capability_id}", headers=headers,
                           json={"capability_id": capability_id, "idempotency_key": key, "payload": payload})
    assert response.status_code == 200, response.text
    result = response.json()
    if result["status"] == "awaiting_confirmation":
        result_response = client.post(f"/v1/capability-records/{result['record_id']}/confirm", headers=headers,
                                      json={"receipt_id": result["data"]["receipt_id"],
                                            "display_hash": result["data"]["display_hash"]})
        assert result_response.status_code == 200, result_response.text
        result = result_response.json()
    if result["status"] == "awaiting_mfa":
        inbox = client.get("/device/v1/inbox", headers={"X-Device-Token": device_token}).json()["messages"]
        challenge = next(item for item in inbox if item["receipt_id"] == result["data"]["receipt_id"])
        verified = client.post(f"/v1/capability-records/{result['record_id']}/verify", headers=headers,
                               json={"otp": challenge["code"]})
        assert verified.status_code == 200, verified.text
        result = verified.json()
    return result


@pytest.mark.parametrize("capability_id", CAPABILITIES)
def test_every_m2_capability_has_http_entrypoint(tmp_path: Path, capability_id: str) -> None:
    app = create_app(f"sqlite:///{tmp_path / f'{capability_id}.db'}")
    client = TestClient(app)
    from agentbank.auth.devices import DeviceService

    device_token = DeviceService(app.state.database).register("user_001")
    client.get("/device/v1/inbox", headers={"X-Device-Token": device_token})
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    if capability_id in {"W01", "W04", "W05", "W06"}:
        execute_capability(client, headers, device_token, "W03", {"answers": [2] * 10},
                           f"http-setup-risk-{capability_id}")
    if capability_id == "W05":
        order = execute_capability(client, headers, device_token, "W04",
                                   {"product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa"},
                                   "http-setup-order-W05")
    if capability_id == "C06":
        execute_capability(client, headers, device_token, "C05", {"card_id": "card_001", "assurance": "mfa"},
                           "http-setup-loss-C06")
    payload = payload_for(capability_id)
    if capability_id == "W05":
        payload["order_id"] = order["data"]["order_id"]
    result = execute_capability(client, headers, device_token, capability_id, payload, f"http-{capability_id}")
    assert result["record_id"]
    assert result["status"] not in {"awaiting_confirmation", "awaiting_mfa", "unimplemented", "stub"}
    data = result["data"]
    if capability_id == "Q01":
        assert data["available_minor"] >= 0 and data["as_of"]
    elif capability_id == "Q02":
        assert all(item["user_id"] == "user_001" for item in data["transactions"])
    elif capability_id in {"T01", "T02", "T03"}:
        assert data["transfer_result"]["status"] == "succeeded"
    elif capability_id == "T04":
        assert data["mandate_id"] and data["schedule_id"]
    elif capability_id == "T05":
        assert data["request_only"] and data["bill_id"]
    elif capability_id == "B01":
        assert data["rows"] and all(row["txn_ids"] for row in data["rows"])
    elif capability_id == "B02":
        assert all(item["txn_ids"] and item["reason_code"] for item in data["anomalies"])
    elif capability_id in {"B03", "B04"}:
        assert data["months"] and all("missing" in month for month in data["months"])
    elif capability_id in {"W01", "W02"}:
        assert data["products"]
    elif capability_id == "W03":
        assert data["level"] and data["expires_on"]
    elif capability_id == "W04":
        assert data["status"] == "subscribed" and data["funds_status"] == "debited"
    elif capability_id == "W05":
        assert data["status"] == "processing" and data["expected_settlement_at"]
    elif capability_id == "W06":
        assert data["source"] == "simulated_bank" and data["as_of"]
    elif capability_id.startswith("C"):
        assert data.get("card_id") and (data.get("status") or data.get("limit_minor") is not None)
    elif capability_id == "S01":
        assert data["subscriptions"]
    elif capability_id == "S02":
        assert data["reminder_id"]
    elif capability_id == "S03":
        assert data["status"] == "cancelled" and data["debit_mandate"] == "revoked"
    elif capability_id == "X01":
        assert data["recommendation_only"] is True
    elif capability_id == "X02":
        assert data["reservation_id"] and data["ledger_balance_unchanged"] is True
    elif capability_id in {"X03", "X04"}:
        assert data["order_id"] and data["status"] == "scheduled"
    elif capability_id == "X05":
        assert data["freeze_status"] == "lost" and data["loss_report_status"] == "succeeded"
