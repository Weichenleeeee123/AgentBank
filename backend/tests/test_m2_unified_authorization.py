from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import sleep

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import AABill, Database, Subscription
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from fastapi.testclient import TestClient
from agentbank.api.app import create_app
from agentbank.auth.devices import DeviceService


def test_write_capability_requires_bound_prepare_and_confirmation(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'unified-auth.db'}")
    load_seed(database)
    service = CapabilityService(database, enforce_prepare=True)
    prepared = service.execute("user_001", "S03", {"subscription_id": "sub_video"}, "auth-s03")
    assert prepared.status == "awaiting_confirmation"
    with database.sessions() as session:
        assert session.get(Subscription, "sub_video").status == "active"
    with pytest.raises(DomainError, match="HASH_MISMATCH"):
        service.confirm_capability("user_001", prepared.record_id, prepared.data["receipt_id"],
                                   "sha256:forged", prepared.data["display_hash"])
    confirmed = service.confirm_capability("user_001", prepared.record_id, prepared.data["receipt_id"],
                                           prepared.data["action_hash"], prepared.data["display_hash"])
    assert confirmed.status == "succeeded"
    with database.sessions() as session:
        assert session.get(Subscription, "sub_video").status == "cancelled"
    with pytest.raises(DomainError, match="RECEIPT_REPLAYED"):
        service.confirm_capability("user_001", prepared.record_id, prepared.data["receipt_id"],
                                   prepared.data["action_hash"], prepared.data["display_hash"])


def test_red_capability_requires_confirmation_then_device_mfa(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'red-auth.db'}")
    load_seed(database)
    service = CapabilityService(database, require_device_mfa=True, enforce_prepare=True)
    service.execute("user_001", "W03", {"answers": [2] * 10}, "red-auth-risk")
    prepared = service.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 1_000}, "red-auth-w04")
    assert prepared.status == "awaiting_confirmation"
    ready = service.confirm_capability("user_001", prepared.record_id, prepared.data["receipt_id"],
                                       prepared.data["action_hash"], prepared.data["display_hash"])
    assert ready.status == "awaiting_mfa"


def test_concurrent_receipt_confirmation_dispatches_only_once(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'concurrent-confirm.db'}")
    load_seed(database)
    service = CapabilityService(database, enforce_prepare=True)
    prepared = service.execute(
        "user_001", "T05", {"total_minor": 10_000, "participant_ids": ["user_001", "user_002"]},
        "concurrent-aa",
    )
    original_dispatch = service._dispatch
    dispatch_count = 0
    dispatch_lock = Lock()

    def delayed_dispatch(user_id, capability_id, payload, idempotency_key):
        nonlocal dispatch_count
        with dispatch_lock:
            dispatch_count += 1
        sleep(0.1)
        return original_dispatch(user_id, capability_id, payload, idempotency_key)

    service._dispatch = delayed_dispatch

    def confirm():
        try:
            return service.confirm_capability(
                "user_001", prepared.record_id, prepared.data["receipt_id"],
                prepared.data["action_hash"], prepared.data["display_hash"],
            ).status
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: confirm(), range(2)))

    assert sorted(outcomes) == ["RECEIPT_REPLAYED", "created"]
    assert dispatch_count == 1
    with database.sessions() as session:
        assert session.query(AABill).filter(AABill.user_id == "user_001").count() == 1


def test_api_write_capability_prepare_confirm_and_mfa_are_one_flow(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'unified-api.db'}")
    device_token = DeviceService(app.state.database).register("user_001")
    client = TestClient(app)
    client.get("/device/v1/inbox", headers={"X-Device-Token": device_token})
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    cancelled = client.post("/v1/capabilities/S03", headers=headers, json={
        "capability_id": "S03", "idempotency_key": "unified-api-s03", "payload": {"subscription_id": "sub_video"},
    }).json()
    assert cancelled["status"] == "awaiting_confirmation"
    confirmed = client.post(f"/v1/capability-records/{cancelled['record_id']}/confirm", headers=headers, json={
        "receipt_id": cancelled["data"]["receipt_id"], "display_hash": cancelled["data"]["display_hash"],
    })
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "succeeded"

    risk = client.post("/v1/risk/submit", headers=headers, json={"answers": [2] * 10}).json()
    assert risk["status"] == "awaiting_confirmation"
    risk_confirm = client.post(f"/v1/capability-records/{risk['record_id']}/confirm", headers=headers,
                               json={"receipt_id": risk["data"]["receipt_id"],
                                     "display_hash": risk["data"]["display_hash"]})
    assert risk_confirm.status_code == 200 and risk_confirm.json()["status"] == "succeeded"
    pending = client.post("/v1/capabilities/W04", headers=headers, json={
        "capability_id": "W04", "idempotency_key": "unified-api-w04", "payload": {
            "product_id": "product_R1", "amount_minor": 1_000,
        },
    }).json()
    assert pending["status"] == "awaiting_confirmation"
    waiting = client.post(f"/v1/capability-records/{pending['record_id']}/confirm", headers=headers, json={
        "receipt_id": pending["data"]["receipt_id"], "display_hash": pending["data"]["display_hash"],
    })
    assert waiting.status_code == 200 and waiting.json()["status"] == "awaiting_mfa"
    code = DeviceService(app.state.database).inbox(device_token)[0]["code"]
    verified = client.post(f"/v1/capability-records/{pending['record_id']}/verify", headers=headers, json={"otp": code})
    assert verified.status_code == 200 and verified.json()["status"] == "succeeded"
