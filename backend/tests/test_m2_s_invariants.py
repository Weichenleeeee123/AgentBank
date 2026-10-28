from pathlib import Path

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database, DebitMandate, Reminder, Subscription
from agentbank.seed.loader import load_seed


def make_service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 's.db'}")
    load_seed(database)
    return CapabilityService(database)


def test_s01_separates_confirmed_and_suspected_subscription_evidence(tmp_path: Path) -> None:
    result = make_service(tmp_path).execute("user_001", "S01", {}, "s01-list")
    statuses = {item["status"] for item in result.data["subscriptions"]}
    assert {"confirmed", "suspected"} <= statuses
    assert all(item["evidence_txn_ids"] for item in result.data["subscriptions"])


def test_s02_reminder_is_persisted_and_idempotent(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = service.execute("user_001", "S02", {"subscription_id": "sub_video", "days": 3}, "s02-same")
    second = service.execute("user_001", "S02", {"subscription_id": "sub_video", "days": 3}, "s02-same")
    assert first.record_id == second.record_id
    with service.database.sessions() as session:
        assert session.query(Reminder).filter(Reminder.id == first.data["reminder_id"]).count() == 1


def test_s03_cancels_subscription_and_revokes_mandate(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.execute("user_001", "S03", {"subscription_id": "sub_video"}, "s03-cancel")
    with service.database.sessions() as session:
        subscription = session.get(Subscription, "sub_video")
        mandate = session.query(DebitMandate).filter(DebitMandate.subscription_id == "sub_video").one()
        assert subscription is not None and subscription.status == "cancelled"
        assert mandate.status == "revoked"
    blocked = service.execute("user_001", "S01", {"action": "simulate_charge", "subscription_id": "sub_video"}, "s03-charge")
    assert blocked.data["charged"] is False
