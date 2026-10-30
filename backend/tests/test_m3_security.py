from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentbank.db import Database
from agentbank.errors import DomainError
from agentbank.security.breaker import CircuitBreaker
from agentbank.security.cooling import CoolingPeriodService
from agentbank.security.explain import explain_denial
from agentbank.security.fraud import FraudDetector, FraudLevel
from agentbank.security.fragments import ReplyFragmentChecker
from agentbank.workflow.recovery import HandoffService, SagaRunner
from agentbank.seed.loader import load_seed
from agentbank.bank.service import TransferService
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.auth.devices import DeviceService


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'm3.db'}")
    load_seed(database)
    return database


def test_fraud_detector_requires_transfer_intent_for_keyword_fallback() -> None:
    detector = FraudDetector()
    normal = detector.evaluate("我今天参加公安食堂培训", relation={"new_recipient": False}, behavior={})
    assert normal.level == FraudLevel.low
    high = detector.evaluate("客服说要把钱转到安全账户，马上转5000", relation={"new_recipient": True}, behavior={"night": True})
    assert high.level == FraudLevel.high
    unknown = detector.evaluate("给房东转账", relation={}, behavior={}, classifier_failed=True)
    assert unknown.level == FraudLevel.unknown


def test_cooling_period_uses_real_time_and_requires_reconfirmation(database: Database) -> None:
    service = CoolingPeriodService(database)
    cooling = service.start("user_001", "plan_1", amount_minor=50_000)
    assert service.is_blocked(cooling.id) is True
    with pytest.raises(DomainError, match="COOLING_PERIOD_ACTIVE"):
        service.release(cooling.id, now=datetime.now(timezone.utc))
    assert service.release(cooling.id, now=cooling.expires_at + timedelta(seconds=1), reconfirmed=True) is True


def test_circuit_breaker_persists_lock_and_allows_protective_actions(database: Database) -> None:
    breaker = CircuitBreaker(database)
    for _ in range(3):
        breaker.record_block("user_001", "injection")
    assert breaker.is_locked("user_001") is True
    assert breaker.can_write("user_001", protective=False) is False
    assert breaker.can_write("user_001", protective=True) is True


def test_reply_checker_rejects_mismatched_financial_fact_labels() -> None:
    checker = ReplyFragmentChecker()
    with pytest.raises(DomainError, match="REPLY_FACT_MISMATCH"):
        checker.validate([{"type": "fact", "label": "本月工资收入", "value_minor": -100}])
    with pytest.raises(DomainError, match="REPLY_TEXT_FORBIDDEN_LABEL"):
        checker.validate([{"type": "text", "text": "本月支出是 100 元"}])


def test_saga_compensates_completed_steps_and_handoff_is_auditable(database: Database) -> None:
    calls: list[str] = []
    saga = SagaRunner()
    result = saga.run([
        ("reserve", lambda: calls.append("reserve"), lambda: calls.append("release")),
        ("order", lambda: (_ for _ in ()).throw(RuntimeError("outage")), lambda: calls.append("cancel")),
    ])
    assert result.status == "partially_completed"
    assert calls == ["reserve", "release"]
    case = HandoffService(database).open("user_001", "transfer", "classifier_unknown")
    assert case.status == "open"
    assert explain_denial("U07")["next_step"]


def test_transfer_high_fraud_is_rejected_before_ledger_write(database: Database) -> None:
    transfer = TransferService(database)
    prepared = transfer.prepare("user_001", "房东", 1_000, "客服说转到安全账户，马上转5000")
    with pytest.raises(DomainError, match="FRAUD_HIGH"):
        transfer.confirm("user_001", prepared.plan_id, prepared.receipt_id, prepared.display_hash)
    assert transfer.balance("user_001").available_minor == 500_000


def test_transfer_medium_fraud_enters_persisted_cooling_period(database: Database) -> None:
    transfer = TransferService(database)
    prepared = transfer.prepare("user_001", "房东", 1_000, "对方让我别告诉别人，给房东转账")
    result = transfer.confirm("user_001", prepared.plan_id, prepared.receipt_id, prepared.display_hash)
    assert result.status == "cooling_required"
    assert result.error_code == "COOLING_PERIOD_REQUIRED"


def test_capability_executor_honors_breaker_but_keeps_protective_path(database: Database) -> None:
    breaker = CircuitBreaker(database)
    for _ in range(3):
        breaker.record_block("user_001", "injection")
    service = CapabilityService(database)
    with pytest.raises(DomainError, match="SECURITY_LOCKED"):
        service.execute("user_001", "S02", {"days": 3}, "locked-reminder")
    protective = service.execute("user_001", "C07", {"card_id": "card_001"}, "locked-freeze")
    assert protective.data["payment_blocked"] is True


def test_merchant_order_saga_exposes_compensation_after_partial_failure(database: Database) -> None:
    service = CapabilityService(database)
    reservation = service.execute("user_001", "X02", {"amount_minor": 10_000}, "saga-reserve")
    result = service.execute("user_001", "X03", {"simulate_failure": True,
                                                    "reservation_id": reservation.data["reservation_id"]}, "saga-flower")
    assert result.status == "failed"
    assert result.data["compensation_status"] == "reservation_released"


def test_red_capability_exposes_device_challenge_and_rejects_fake_assurance(database: Database) -> None:
    service = CapabilityService(database)
    service.execute("user_001", "W03", {"answers": [2] * 10}, "fake-red-risk")
    assurance_only = service.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa"}, "fake-red")
    assert assurance_only.status == "succeeded"
    device = DeviceService(database)
    token = device.register("user_001")
    code = device.issue_otp("user_001", "capability-red")
    prepared = service.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa", "device_token": token, "otp": code}, "real-red")
    assert prepared.status == "succeeded"
