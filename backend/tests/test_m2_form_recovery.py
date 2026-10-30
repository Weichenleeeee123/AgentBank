from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app
from agentbank.db import CapabilityRecord, ReceiptRecord


def setup(tmp_path: Path):
    app = create_app(f"sqlite:///{tmp_path / 'form-recovery.db'}")
    client = TestClient(app)
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    return app, client, {"Authorization": f"Bearer {token}"}


def test_form_can_select_same_name_payee_and_revise_invalidates_old_authorization(tmp_path: Path) -> None:
    app, client, headers = setup(tmp_path)
    candidates = client.get("/v1/payees?name=张三", headers=headers)
    assert candidates.status_code == 200
    items = candidates.json()["payees"]
    assert len(items) == 2
    assert {item["phone_tail"] for item in items} == {"0001", "0002"}
    assert all("phone" not in item for item in items)
    selected = next(item for item in items if not item["is_new"])
    prepared_response = client.post("/v1/forms/transfer-name", headers=headers, json={
        "recipient_id": selected["id"], "amount_minor": 1000, "idempotency_key": "form-selected"})
    assert prepared_response.status_code == 200, prepared_response.text
    prepared = prepared_response.json()
    assert prepared["status"] == "awaiting_confirmation"
    revised = client.post(f"/v1/capability-records/{prepared['record_id']}/revise", headers=headers,
                          json={"payload": {"recipient_id": selected["id"], "amount_minor": 2000},
                                "idempotency_key": "form-revised"})
    assert revised.status_code == 200, revised.text
    assert revised.json()["data"]["payload"]["amount_minor"] == 2000
    with app.state.database.sessions() as session:
        assert session.get(CapabilityRecord, prepared["record_id"]).status == "invalidated"
        assert session.get(ReceiptRecord, prepared["data"]["receipt_id"]).status == "invalidated"
    old_confirm = client.post(f"/v1/capability-records/{prepared['record_id']}/confirm", headers=headers,
                              json={"receipt_id": prepared["data"]["receipt_id"],
                                    "display_hash": prepared["data"]["display_hash"]})
    assert old_confirm.status_code != 200


def test_form_revision_is_owner_scoped(tmp_path: Path) -> None:
    _app, client, headers = setup(tmp_path)
    prepared = client.post("/v1/forms/transfer-name", headers=headers,
                            json={"recipient_name": "房东", "amount_minor": 1000}).json()
    other = client.post("/v1/sessions", json={"user_id": "user_002"}).json()["access_token"]
    response = client.post(f"/v1/capability-records/{prepared['record_id']}/revise",
                           headers={"Authorization": f"Bearer {other}"},
                           json={"payload": {"recipient_name": "房东", "amount_minor": 2000}})
    assert response.status_code == 400


def test_form_idempotency_envelope_is_not_a_business_payload_field(tmp_path: Path) -> None:
    _app, client, headers = setup(tmp_path)
    payload = {"recipient_name": "房东", "amount_minor": 1000, "idempotency_key": "fixed-form-key"}
    first = client.post("/v1/forms/transfer-name", headers=headers, json=payload)
    second = client.post("/v1/forms/transfer-name", headers=headers, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["record_id"] == second.json()["record_id"]
