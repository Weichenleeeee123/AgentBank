import json
from pathlib import Path

import pytest
from sqlalchemy import select

from agentbank.auth.devices import DeviceService
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import (
    CapabilityRecord,
    Card,
    Database,
    InvestmentOrder,
    ReceiptConsumption,
    ReceiptRecord,
    Subscription,
)
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'atomic-capability.db'}")
    load_seed(db)
    return db


def _prepare_s03(service: CapabilityService) -> tuple[CapabilityRecord, dict[str, object]]:
    prepared = service.execute("user_001", "S03", {"subscription_id": "sub_video"}, "atomic-s03")
    with service.database.sessions() as session:
        record = session.get(CapabilityRecord, prepared.record_id)
        assert record is not None
        return record, prepared.data


def test_confirmation_recomputes_hashes_from_persisted_safe_payload(database: Database) -> None:
    service = CapabilityService(database, enforce_prepare=True)
    record, data = _prepare_s03(service)
    stored = json.loads(record.data_json)
    stored["payload"]["subscription_id"] = "sub_music"
    with database.sessions.begin() as session:
        session.get(CapabilityRecord, record.id).data_json = json.dumps(stored, sort_keys=True)

    with pytest.raises(DomainError, match="HASH_MISMATCH"):
        service.confirm_capability("user_001", record.id, str(data["receipt_id"]),
                                   str(data["action_hash"]), str(data["display_hash"]))

    with database.sessions() as session:
        assert session.get(Subscription, "sub_video").status == "active"


def test_audit_failure_rolls_back_business_receipt_and_status_then_same_receipt_retries(
    database: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CapabilityService(database, enforce_prepare=True)
    record, data = _prepare_s03(service)
    original_append = service.audit.append

    def fail_after_business(session, trace_id, subject_id, event_type, payload):
        if event_type == "capability.S03.confirmed":
            raise RuntimeError("injected audit failure")
        return original_append(session, trace_id, subject_id, event_type, payload)

    monkeypatch.setattr(service.audit, "append", fail_after_business)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        service.confirm_capability("user_001", record.id, str(data["receipt_id"]),
                                   str(data["action_hash"]), str(data["display_hash"]))

    with database.sessions() as session:
        assert session.get(Subscription, "sub_video").status == "active"
        persisted = session.get(CapabilityRecord, record.id)
        receipt = session.get(ReceiptRecord, str(data["receipt_id"]))
        assert persisted.status == "awaiting_confirmation"
        assert receipt.status == "pending"
        assert session.get(ReceiptConsumption, (receipt.id, record.id)) is None

    monkeypatch.setattr(service.audit, "append", original_append)
    result = service.confirm_capability("user_001", record.id, str(data["receipt_id"]),
                                        str(data["action_hash"]), str(data["display_hash"]))
    assert result.status == "succeeded"
    with database.sessions() as session:
        assert session.get(Subscription, "sub_video").status == "cancelled"
        assert session.get(ReceiptRecord, str(data["receipt_id"])).status == "consumed"
        assert session.get(ReceiptConsumption, (str(data["receipt_id"]), record.id)) is not None


def test_mfa_business_audit_and_receipt_rollback_after_device_verification(
    database: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CapabilityService(database, require_device_mfa=True, enforce_prepare=True)
    CapabilityService(database).execute("user_001", "W03", {"answers": [1] * 10}, "atomic-w04-risk")
    device = DeviceService(database)
    token = device.register("user_001")
    service.device_tokens["user_001"] = token
    prepared = service.execute("user_001", "W04", {
        "product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa",
    }, "atomic-w04")
    waiting = service.confirm_capability("user_001", prepared.record_id,
                                         str(prepared.data["receipt_id"]),
                                         str(prepared.data["action_hash"]),
                                         str(prepared.data["display_hash"]))
    receipt_id = str(waiting.data["receipt_id"])
    code = next(item["code"] for item in device.inbox(token) if item["receipt_id"] == receipt_id)
    original_append = service.audit.append

    def fail_final_audit(session, trace_id, subject_id, event_type, payload):
        if event_type == "capability.W04.mfa_completed":
            raise RuntimeError("injected MFA audit failure")
        return original_append(session, trace_id, subject_id, event_type, payload)

    monkeypatch.setattr(service.audit, "append", fail_final_audit)
    with pytest.raises(RuntimeError, match="injected MFA audit failure"):
        service.verify_mfa("user_001", waiting.record_id, code)

    with database.sessions() as session:
        assert session.scalar(select(InvestmentOrder).where(InvestmentOrder.user_id == "user_001")) is None
        record = session.get(CapabilityRecord, waiting.record_id)
        receipt = session.get(ReceiptRecord, receipt_id)
        assert record.status == "awaiting_mfa"
        assert receipt.status == "pending"
        assert session.get(ReceiptConsumption, (receipt_id, waiting.record_id)) is None

    monkeypatch.setattr(service.audit, "append", original_append)
    retry_code = device.issue_otp("user_001", receipt_id)
    result = service.verify_mfa("user_001", waiting.record_id, retry_code)
    assert result.status == "succeeded"
    with database.sessions() as session:
        assert session.scalar(select(InvestmentOrder).where(InvestmentOrder.user_id == "user_001")) is not None
        assert session.get(ReceiptRecord, receipt_id).status == "consumed"
        assert session.get(ReceiptConsumption, (receipt_id, waiting.record_id)) is not None


def test_c08_confirmation_hash_uses_redacted_payload(database: Database) -> None:
    service = CapabilityService(database, enforce_prepare=True)
    prepared = service.execute("user_001", "C08", {
        "card_id": "card_001", "new_pin": "1234", "assurance": "mfa",
    }, "atomic-c08")

    assert prepared.data["payload"]["new_pin"] == "[REDACTED]"
    result = service.confirm_capability("user_001", prepared.record_id,
                                        str(prepared.data["receipt_id"]),
                                        str(prepared.data["action_hash"]),
                                        str(prepared.data["display_hash"]))

    assert result.status == "succeeded"
    assert "new_pin" not in result.data.get("payload", {})


def test_x05_staged_mfa_hash_is_recomputed_before_second_stage(database: Database) -> None:
    service = CapabilityService(database, enforce_prepare=True)
    device = DeviceService(database)
    token = device.register("user_001")
    service.device_tokens["user_001"] = token
    prepared = service.execute("user_001", "X05", {"card_id": "card_001"}, "atomic-x05")
    waiting = service.confirm_capability("user_001", prepared.record_id,
                                         str(prepared.data["receipt_id"]),
                                         str(prepared.data["action_hash"]),
                                         str(prepared.data["display_hash"]))
    receipt_id = str(waiting.data["receipt_id"])
    code = next(item["code"] for item in device.inbox(token) if item["receipt_id"] == receipt_id)

    stored = dict(waiting.data)
    stored["action_hash"] = "sha256:tampered"
    with database.sessions.begin() as session:
        session.get(CapabilityRecord, waiting.record_id).data_json = json.dumps(stored, sort_keys=True)
    with pytest.raises(DomainError, match="HASH_MISMATCH"):
        service.verify_mfa("user_001", waiting.record_id, code)

    with database.sessions() as session:
        assert session.get(Card, "card_001").status == "frozen"
        assert session.get(ReceiptRecord, receipt_id).status == "pending"
    stored["action_hash"] = waiting.data["action_hash"]
    with database.sessions.begin() as session:
        session.get(CapabilityRecord, waiting.record_id).data_json = json.dumps(stored, sort_keys=True)
    result = service.verify_mfa("user_001", waiting.record_id, code)

    assert result.status == "succeeded"
    with database.sessions() as session:
        assert session.get(Card, "card_001").status == "lost"
