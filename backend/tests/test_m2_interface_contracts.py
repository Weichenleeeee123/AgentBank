from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_m2_forms_operations_annual_aa_and_eval_runs(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'm2-interfaces.db'}")
    client = TestClient(app)
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    balance = client.post("/v1/forms/balance", headers=headers, json={})
    assert balance.status_code == 200
    annual = client.get("/v1/reports/annual?year=2026", headers=headers)
    assert annual.status_code == 200 and len(annual.json()["months"]) == 12
    bill = client.post("/v1/aa/bills", headers=headers, json={"total_minor": 10_000,
                     "participant_ids": ["user_001", "user_002", "user_003"], "idempotency_key": "api-aa"})
    prepared = bill.json()
    assert prepared["status"] == "awaiting_confirmation"
    confirmed = client.post(f"/v1/capability-records/{prepared['record_id']}/confirm", headers=headers,
                            json={"receipt_id": prepared["data"]["receipt_id"],
                                  "display_hash": prepared["data"]["display_hash"]}).json()
    bill_id = confirmed["data"]["bill_id"]
    status = client.get(f"/v1/aa/bills/{bill_id}", headers=headers)
    assert status.status_code == 200
    paid = client.post(f"/v1/aa/bills/{bill_id}/pay", headers={"Authorization": f"Bearer {client.post('/v1/sessions', json={'user_id':'user_002'}).json()['access_token']}"}, json={})
    assert paid.status_code == 200
    run = client.post("/v1/eval/runs", headers=headers)
    assert run.status_code == 200 and run.json()["total"] >= 100


def test_operation_query_and_plan_sse_are_owner_checked(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'm2-op-sse.db'}")
    client = TestClient(app)
    token1 = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    token2 = client.post("/v1/sessions", json={"user_id": "user_002"}).json()["access_token"]
    headers1, headers2 = {"Authorization": f"Bearer {token1}"}, {"Authorization": f"Bearer {token2}"}
    prepared = client.post("/v1/transfers/prepare", headers=headers1, json={"recipient_name": "房东", "amount_minor": 1000}).json()
    result = client.post("/v1/plans/" + prepared["plan_id"] + "/confirm", headers=headers1,
                         json={"receipt_id": prepared["receipt_id"], "display_hash": prepared["display_hash"]}).json()
    operation = client.get(f"/v1/operations/{result['operation_id']}", headers=headers1)
    assert operation.status_code == 200 and operation.json()["status"] == "succeeded"
    denied = client.get(f"/v1/operations/{result['operation_id']}", headers=headers2)
    assert denied.status_code == 400
    plan = client.post("/v1/birthday/plans", headers=headers1, json={"event_id": "event_birthday"}).json()
    events = client.get(f"/v1/plans/{plan['plan_id']}/events", headers=headers1)
    assert events.status_code == 200 and "event: snapshot" in events.text
