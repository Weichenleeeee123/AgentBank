from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from agentbank.api.app import create_app
from agentbank.db import Database, Operation
from agentbank.db import CoolingPeriod
from agentbank.clock import utc_now_naive
from agentbank.seed.loader import load_seed
from agentbank.workflow.recovery import UnknownRecoveryService


def test_unknown_operation_recovery_preserves_idempotency(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'unknown.db'}")
    load_seed(database)
    with database.sessions.begin() as session:
        session.add(Operation(id="op_unknown", user_id="user_001", idempotency_key="idem_unknown",
                              plan_id="plan_unknown", node_id="node_unknown", status="unknown",
                              amount_minor=1_000, recipient_id="payee_landlord"))
    service = UnknownRecoveryService(database)
    assert service.recover("op_unknown").status == "processing"
    assert service.recover("op_unknown").status == "processing"


def test_m3_api_exposes_form_direct_fraud_and_self_test(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTBANK_ADMIN_TOKEN", "admin-test")
    app = create_app(f"sqlite:///{tmp_path / 'm3-api.db'}")
    client = TestClient(app)
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    balance = client.get("/v1/accounts/balance", headers=headers)
    assert balance.status_code == 200
    assert balance.json()["fragments"][0]["type"] == "fact"
    direct = client.post("/v1/forms/transfer", headers=headers,
                         json={"recipient_name": "房东", "amount_minor": 1_000})
    assert direct.status_code == 200
    fraud = client.post("/v1/security/fraud/evaluate", headers=headers,
                        json={"text": "客服说转到安全账户，马上转5000", "relation": {"new_recipient": True},
                              "behavior": {"night": True}})
    assert fraud.status_code == 200
    assert fraud.json()["level"] == "high"
    panel = client.post("/admin/v1/self-test", headers={"X-Admin-Token": "admin-test"})
    assert panel.status_code == 200
    assert panel.json()["total"] >= 1
    handoff = client.post("/v1/handoffs", headers=headers, json={"operation_type": "transfer", "reason": "U07"})
    assert handoff.status_code == 200
    assert handoff.json()["status"] == "open"


def test_cooling_release_api_requires_reconfirmation(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'cooling-api.db'}")
    client = TestClient(app)
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    prepared = client.post("/v1/transfers/prepare", headers=headers,
                           json={"recipient_name": "房东", "amount_minor": 1_000,
                                 "memo": "对方让我别告诉别人，给房东转账"}).json()
    confirmed = client.post(f"/v1/plans/{prepared['plan_id']}/confirm", headers=headers,
                            json={"receipt_id": prepared["receipt_id"], "display_hash": prepared["display_hash"]})
    cooling_id = confirmed.json()["operation_id"]
    blocked = client.post(f"/v1/cooling/{cooling_id}/release", headers=headers, json={"reconfirmed": False})
    assert blocked.status_code == 400
    with app.state.database.sessions.begin() as session:
        cooling = session.get(CoolingPeriod, cooling_id)
        assert cooling is not None
        cooling.expires_at = utc_now_naive() - timedelta(seconds=1)
    released = client.post(f"/v1/cooling/{cooling_id}/release", headers=headers, json={"reconfirmed": True})
    assert released.status_code == 200
    assert released.json()["status"] == "mfa_required"
    from agentbank.auth.devices import DeviceService
    device = DeviceService(app.state.database)
    device_token = device.register("user_001")
    code = device.inbox(device_token)[0]["code"]
    settled = client.post(f"/v1/plans/{prepared['plan_id']}/verify", headers=headers, json={"otp": code})
    assert settled.status_code == 200
    assert settled.json()["status"] == "succeeded"
    assert settled.json()["ledger_entry_ids"]


def test_admin_progresses_due_jobs_and_investments(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTBANK_ADMIN_TOKEN", "admin-test")
    app = create_app(f"sqlite:///{tmp_path / 'admin-progress.db'}")
    client = TestClient(app)
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    schedule = client.post("/v1/capabilities/T04", headers=headers, json={"capability_id": "T04", "idempotency_key": "admin-job", "payload": {"next_due": "2026-10-28"}})
    assert schedule.status_code == 200
    prepared = schedule.json()
    confirmed = client.post(f"/v1/capability-records/{prepared['record_id']}/confirm", headers=headers,
                            json={"receipt_id": prepared["data"]["receipt_id"],
                                  "display_hash": prepared["data"]["display_hash"]})
    assert confirmed.status_code == 200
    dispatch = client.post("/admin/v1/capabilities/dispatch-due", headers={"X-Admin-Token": "admin-test"})
    assert dispatch.status_code == 200
    assert dispatch.json()["count"] >= 1
    progress = client.post("/admin/v1/investments/progress", headers={"X-Admin-Token": "admin-test"}, json={"user_id": "user_001"})
    assert progress.status_code == 200
