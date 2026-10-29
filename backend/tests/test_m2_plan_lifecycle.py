from pathlib import Path
from datetime import date, timedelta

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Account, Database, Event, LedgerEntry, MerchantOrder, Operation, PlanRecord, ReceiptRecord, Reservation
from agentbank.errors import DomainError
from agentbank.clock import BusinessClock
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService


def setup_plan(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    load_seed(database)
    return database, BusinessPlanService(database)


def execute(service, database, plan_id):
    with database.sessions() as session:
        plan = session.get(PlanRecord, plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan_id).one()
        receipt_id, display_hash = receipt.id, plan.display_hash
    return service.execute_plan("user_001", plan_id, receipt_id, display_hash)


def test_birthday_orders_use_the_event_date_confirmed_in_plan(tmp_path: Path) -> None:
    database, service = setup_plan(tmp_path)
    with database.sessions.begin() as session:
        session.get(Event, "event_birthday").event_date = "2027-02-20"
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    assert any(item["value"] == "2027-02-20" for item in plan.display)
    execute(service, database, plan.plan_id)
    with database.sessions() as session:
        orders = session.query(MerchantOrder).all()
        assert len(orders) == 2
        assert {order.delivery_date for order in orders} == {"2027-02-20"}


def test_repeated_plan_revisions_keep_receipts_and_returned_revision_in_sync(tmp_path: Path) -> None:
    database, service = setup_plan(tmp_path)
    original = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    revised = service.revise_birthday_plan("user_001", original.plan_id, {"reserve_minor": 90_000})
    revised_again = service.revise_birthday_plan(
        "user_001", revised.plan_id, {"reserve_minor": 80_000, "event_date": "2027-03-01"}
    )
    assert revised_again.revision == 3
    with database.sessions() as session:
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == revised_again.plan_id).one()
        assert receipt.plan_revision == 3
    assert execute(service, database, revised_again.plan_id)["status"] == "succeeded"
    with database.sessions() as session:
        assert {order.delivery_date for order in session.query(MerchantOrder).all()} == {"2027-03-01"}


def test_pause_after_reservation_stops_orders_and_resume_reuses_reservation(tmp_path: Path, monkeypatch) -> None:
    database, service = setup_plan(tmp_path)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    original_execute = CapabilityService.execute

    def pause_after_reserve(capabilities, user_id, capability_id, payload, key):
        result = original_execute(capabilities, user_id, capability_id, payload, key)
        if capability_id == "X02":
            service.pause(user_id, plan.plan_id)
        return result

    monkeypatch.setattr(CapabilityService, "execute", pause_after_reserve)
    assert execute(service, database, plan.plan_id)["status"] == "paused"
    with database.sessions() as session:
        assert session.query(MerchantOrder).count() == 0
        assert session.query(Reservation).count() == 1
    service.resume("user_001", plan.plan_id)
    assert execute(service, database, plan.plan_id)["status"] == "succeeded"
    with database.sessions() as session:
        reservation = session.query(Reservation).one()
        assert {order.reservation_id for order in session.query(MerchantOrder).all()} == {reservation.id}
        assert session.query(Account).filter(Account.user_id == "user_001").one().available_minor == 400_000


def test_cancelling_paused_plan_releases_reservation_and_invalidates_receipt(tmp_path: Path, monkeypatch) -> None:
    database, service = setup_plan(tmp_path)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    original_execute = CapabilityService.execute

    def pause_after_reserve(capabilities, user_id, capability_id, payload, key):
        result = original_execute(capabilities, user_id, capability_id, payload, key)
        if capability_id == "X02":
            service.pause(user_id, plan.plan_id)
        return result

    monkeypatch.setattr(CapabilityService, "execute", pause_after_reserve)
    execute(service, database, plan.plan_id)
    service.cancel("user_001", plan.plan_id)
    with database.sessions() as session:
        assert session.query(Reservation).one().status == "released"
        assert session.query(Account).filter(Account.user_id == "user_001").one().available_minor == 500_000
        assert session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one().status == "invalidated"
    with pytest.raises(DomainError):
        execute(service, database, plan.plan_id)


def test_birthday_dispatch_posts_each_order_and_releases_unused_budget(tmp_path: Path) -> None:
    database, service = setup_plan(tmp_path)
    today = date.fromisoformat(BusinessClock().business_date())
    event_date = (today + timedelta(days=2)).isoformat()
    with database.sessions.begin() as session:
        session.get(Event, "event_birthday").event_date = event_date
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    assert execute(service, database, plan.plan_id)["status"] == "succeeded"

    dispatched = CapabilityService(database).dispatch_due("user_001")

    assert len(dispatched) == 2
    with database.sessions() as session:
        account = session.query(Account).filter(Account.user_id == "user_001").one()
        reservation = session.query(Reservation).filter(Reservation.plan_id == plan.plan_id).one()
        orders = session.query(MerchantOrder).all()
        assert all(order.status == "delivered" for order in orders)
        assert reservation.status == "released"
        assert account.available_minor == 490_000
        assert account.ledger_balance_minor == 490_000
        operations = session.query(Operation).filter(Operation.plan_id == plan.plan_id).all()
        assert len(operations) == 2
        for operation in operations:
            entries = session.query(LedgerEntry).filter(LedgerEntry.operation_id == operation.id).all()
            assert len(entries) == 2 and sum(entry.amount_minor for entry in entries) == 0
