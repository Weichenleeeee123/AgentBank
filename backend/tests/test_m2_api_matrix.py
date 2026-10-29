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
        return {"order_id": "unknown-order", "assurance": "mfa"}
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


@pytest.mark.parametrize("capability_id", CAPABILITIES)
def test_every_m2_capability_has_http_entrypoint(tmp_path: Path, capability_id: str) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / f'{capability_id}.db'}"))
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    if capability_id in {"W01", "W04", "W05", "W06"}:
        risk = client.post("/v1/risk/submit", headers=headers, json={"answers": [2] * 10})
        assert risk.status_code == 200
        prepared_risk = risk.json()
        if prepared_risk.get("status") == "awaiting_confirmation":
            risk_confirm = client.post(f"/v1/capability-records/{prepared_risk['record_id']}/confirm", headers=headers,
                                       json={"receipt_id": prepared_risk["data"]["receipt_id"],
                                             "display_hash": prepared_risk["data"]["display_hash"]})
            assert risk_confirm.status_code == 200
    response = client.post(f"/v1/capabilities/{capability_id}", headers={"Authorization": f"Bearer {token}"},
                           json={"capability_id": capability_id, "idempotency_key": f"http-{capability_id}",
                                 "payload": payload_for(capability_id)})
    assert response.status_code == 200, response.text
    assert response.json()["record_id"]
