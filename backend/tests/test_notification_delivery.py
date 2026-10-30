from datetime import datetime, timezone
from pathlib import Path

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.clock import BusinessClock
from agentbank.db import (Database, DeliveryAttempt, Event, LedgerEntry, OutboxEvent,
                          Reservation, Transaction, UserNotification)
from agentbank.notifications import OutboxDeliveryService
from agentbank.scheduler import BusinessScheduler
from agentbank.seed.loader import load_seed


def setup(tmp_path: Path) -> tuple[Database, CapabilityService]:
    database = Database(f"sqlite:///{tmp_path / 'notifications.db'}")
    load_seed(database)
    clock = BusinessClock(datetime(2026, 10, 14, tzinfo=timezone.utc))
    return database, CapabilityService(database, clock)


def test_failed_reminder_delivery_stays_pending_and_retry_creates_one_notice(tmp_path: Path) -> None:
    database, capabilities = setup(tmp_path)
    capabilities.execute("user_001", "S02", {"subscription_id": "sub_video", "days": 3}, "delivery-retry")
    capabilities.clock.advance_days(29)
    capabilities.dispatch_reminders("user_001")
    attempts = 0

    def delivery(_event: dict[str, str]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("inbox temporarily unavailable")

    service = OutboxDeliveryService(database, delivery)
    first = service.deliver("user_001")
    assert first[0]["status"] == "retry_pending"
    with database.sessions() as session:
        event = session.query(OutboxEvent).one()
        assert event.status == "pending"
        assert session.query(UserNotification).count() == 0
        failed = session.query(DeliveryAttempt).one()
        assert failed.status == "failed" and failed.attempt_number == 1

    second = service.deliver("user_001")
    assert second[0]["status"] == "delivered"
    assert service.deliver("user_001") == []
    with database.sessions() as session:
        event = session.query(OutboxEvent).one()
        assert event.status == "delivered"
        assert session.query(UserNotification).count() == 1
        assert session.query(DeliveryAttempt).count() == 2
        assert session.query(DeliveryAttempt).filter(DeliveryAttempt.status == "delivered").count() == 1


def test_birthday_discovery_requires_exact_30_day_window_and_prior_year_history(tmp_path: Path) -> None:
    database, capabilities = setup(tmp_path)
    with database.sessions.begin() as session:
        event = session.get(Event, "event_birthday")
        event.event_date = "2026-11-13"
    before_balance = capabilities._account("user_001").available_minor
    scheduler = BusinessScheduler(capabilities)

    first = scheduler.discover_birthdays()
    assert len(first) == 1
    assert first[0]["recommendation_only"] is True
    assert first[0]["evidence"]
    assert scheduler.discover_birthdays() == []
    with database.sessions() as session:
        notice = session.query(UserNotification).one()
        assert notice.event_type == "birthday.recommendation"
        assert "recommendation_only" in notice.body
        assert session.query(OutboxEvent).count() == 0
        assert session.query(Reservation).count() == 0
        assert session.query(LedgerEntry).count() == 0
    assert capabilities._account("user_001").available_minor == before_balance


def test_birthday_discovery_does_not_use_same_year_or_other_date_clues(tmp_path: Path) -> None:
    database, capabilities = setup(tmp_path)
    with database.sessions.begin() as session:
        session.get(Event, "event_birthday").event_date = "2026-11-13"
        flower = session.get(Transaction, "txn_history_flower")
        flower.posted_at = "2026-03-01"
        cake = session.get(Transaction, "txn_history_cake")
        cake.posted_at = "2025-03-02"
    assert BusinessScheduler(capabilities).discover_birthdays() == []
    with database.sessions() as session:
        assert session.query(UserNotification).count() == 0
