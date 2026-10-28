from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import AABill, AAShare, CardApplication, Database, DebitMandate, InvestmentOrder, MerchantOrder, Position, Reminder, ScheduledJob
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


@pytest.fixture()
def service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'domain.db'}")
    load_seed(database)
    return CapabilityService(database)


def test_aa_create_persists_bill_and_shares(service: CapabilityService) -> None:
    result = service.execute("user_001", "T05", {"total_minor": 10_000, "participant_ids": ["user_001", "user_002", "user_003"]}, "aa-domain")
    bill_id = result.data["bill_id"]
    with service.database.sessions() as session:
        assert session.get(AABill, bill_id).status == "open"
        assert len(session.query(AAShare).filter(AAShare.bill_id == bill_id).all()) == 2
    status = service.execute("user_001", "T05", {"bill_id": bill_id, "action": "status"}, "aa-domain-status")
    assert status.data["bill_id"] == bill_id


def test_investment_order_and_position_are_persisted(service: CapabilityService) -> None:
    service.execute("user_001", "W03", {"answers": [2] * 10}, "risk-domain")
    result = service.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 10_000, "assurance": "mfa"}, "investment-domain")
    order_id = result.data["order_id"]
    with service.database.sessions() as session:
        assert session.get(InvestmentOrder, order_id).status == "subscribed"
        assert session.query(Position).filter(Position.user_id == "user_001").count() == 1
    redeemed = service.execute("user_001", "W05", {"order_id": order_id, "assurance": "mfa"}, "redeem-domain")
    assert redeemed.data["status"] == "processing"


def test_card_application_reminder_debit_mandate_and_merchant_order_persist(service: CapabilityService) -> None:
    card = service.execute("user_001", "C01", {"virtual": False, "assurance": "mfa"}, "card-domain")
    with service.database.sessions() as session:
        assert session.query(CardApplication).filter(CardApplication.card_id == card.data["card_id"]).count() == 1
    reminder = service.execute("user_001", "S02", {"subscription_id": "sub_video", "days": 3}, "reminder-domain")
    with service.database.sessions() as session:
        assert session.get(Reminder, reminder.data["reminder_id"]).status == "scheduled"
    service.execute("user_001", "S03", {"subscription_id": "sub_video"}, "cancel-domain")
    with service.database.sessions() as session:
        mandate = session.query(DebitMandate).filter(DebitMandate.subscription_id == "sub_video").one()
        assert mandate.status == "revoked"
    order = service.execute("user_001", "X03", {"delivery_date": "2026-11-13"}, "merchant-domain")
    with service.database.sessions() as session:
        assert session.get(MerchantOrder, order.data["order_id"]).status == "scheduled"


def test_schedule_is_persisted_as_a_job(service: CapabilityService) -> None:
    result = service.execute("user_001", "T04", {"recipient_name": "房东", "amount_minor": 1000, "max_uses": 1}, "job-domain")
    with service.database.sessions() as session:
        jobs = session.query(ScheduledJob).filter(ScheduledJob.schedule_id == result.data["schedule_id"]).all()
        assert len(jobs) == 1
        assert jobs[0].status == "scheduled"


def test_card_limit_increase_requires_mfa_and_decrease_is_yellow(service: CapabilityService) -> None:
    lowered = service.execute("user_001", "C02", {"card_id": "card_001", "limit_minor": 90_000}, "limit-lower")
    assert lowered.data["limit_minor"] == 90_000
    with pytest.raises(DomainError, match="MFA_REQUIRED"):
        service.execute("user_001", "C02", {"card_id": "card_001", "limit_minor": 110_000}, "limit-up-missing")
    raised = service.execute("user_001", "C02", {"card_id": "card_001", "limit_minor": 110_000, "assurance": "mfa"}, "limit-up-mfa")
    assert raised.data["limit_minor"] == 110_000
