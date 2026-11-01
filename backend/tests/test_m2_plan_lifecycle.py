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


def test_birthday_quote_nodes_precede_reservation_and_stale_quote_never_holds_funds(tmp_path: Path) -> None:
    database, service = setup_plan(tmp_path)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    with database.sessions() as session:
        record = session.get(PlanRecord, plan.plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one()
        assert record is not None
        nodes = session.query(__import__("agentbank.db", fromlist=["PlanNodeRecord"]).PlanNodeRecord).filter(
            __import__("agentbank.db", fromlist=["PlanNodeRecord"]).PlanNodeRecord.plan_id == plan.plan_id
        ).all()
        assert {node.tool for node in nodes} >= {"merchant.quote", "funds.reserve"}
    CapabilityService(database).update_merchant_catalog("flower", price_minor=9000)

    outcome = service.execute_plan("user_001", plan.plan_id, receipt.id, record.display_hash)

    assert outcome["status"] == "partially_completed"
    assert outcome["nodes"][1]["status"] == "failed"
    with database.sessions() as session:
        assert session.query(Reservation).filter(Reservation.plan_id == plan.plan_id).count() == 0
        assert session.query(MerchantOrder).count() == 0
        assert session.query(Account).filter(Account.user_id == "user_001").one().available_minor == 500_000


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
    assert execute(service, database, revised_again.plan_id)["status"] == "waiting_trigger"
    with database.sessions() as session:
        assert {order.delivery_date for order in session.query(MerchantOrder).all()} == {"2027-03-01"}


def test_plan_revision_invalidates_old_receipt_and_requires_new_confirmation(tmp_path: Path) -> None:
    database, service = setup_plan(tmp_path)
    original = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    with database.sessions() as session:
        old_receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == original.plan_id).one()
        old_hash = session.get(PlanRecord, original.plan_id).display_hash
    revised = service.revise_birthday_plan("user_001", original.plan_id, {"reserve_minor": 90_000})
    with pytest.raises(DomainError, match="RECEIPT_REPLAYED"):
        service.execute_plan("user_001", original.plan_id, old_receipt.id, old_hash)
    assert revised.revision == 2


def test_pause_after_reservation_stops_orders_and_resume_reuses_reservation(tmp_path: Path, monkeypatch) -> None:
    database, service = setup_plan(tmp_path)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    original_execute = CapabilityService.execute

    def pause_after_reserve(capabilities, user_id, capability_id, payload, key, **kwargs):
        result = original_execute(capabilities, user_id, capability_id, payload, key, **kwargs)
        if capability_id == "X02":
            service.pause(user_id, plan.plan_id)
        return result

    monkeypatch.setattr(CapabilityService, "execute", pause_after_reserve)
    assert execute(service, database, plan.plan_id)["status"] == "paused"
    with database.sessions() as session:
        assert session.query(MerchantOrder).count() == 0
        assert session.query(Reservation).count() == 1
    service.resume("user_001", plan.plan_id)
    assert execute(service, database, plan.plan_id)["status"] == "waiting_trigger"
    with database.sessions() as session:
        reservation = session.query(Reservation).one()
        assert {order.reservation_id for order in session.query(MerchantOrder).all()} == {reservation.id}
        assert session.query(Account).filter(Account.user_id == "user_001").one().available_minor == 400_000


def test_cancelling_paused_plan_releases_reservation_and_invalidates_receipt(tmp_path: Path, monkeypatch) -> None:
    database, service = setup_plan(tmp_path)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    original_execute = CapabilityService.execute

    def pause_after_reserve(capabilities, user_id, capability_id, payload, key, **kwargs):
        result = original_execute(capabilities, user_id, capability_id, payload, key, **kwargs)
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
    assert execute(service, database, plan.plan_id)["status"] == "waiting_trigger"

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


def test_restarted_service_pauses_running_plan_and_resume_recovers_committed_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = setup_plan(tmp_path)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    original = CapabilityService.execute
    interrupted = False

    def crash_after_reservation(capabilities, user_id, capability_id, payload, key, **kwargs):
        nonlocal interrupted
        result = original(capabilities, user_id, capability_id, payload, key, **kwargs)
        if capability_id == "X02" and not interrupted:
            interrupted = True
            raise RuntimeError("simulated process termination")
        return result

    monkeypatch.setattr(CapabilityService, "execute", crash_after_reservation)
    with pytest.raises(RuntimeError, match="simulated process termination"):
        execute(service, database, plan.plan_id)

    restarted = BusinessPlanService(database)
    assert restarted.recover_interrupted() == [plan.plan_id]
    assert restarted.resume("user_001", plan.plan_id)["status"] == "awaiting_confirmation"
    receipt_id, display_hash = _receipt_for(database, plan.plan_id)
    resumed = restarted.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)
    assert resumed["status"] == "waiting_trigger"
    with database.sessions() as session:
        assert session.query(Reservation).filter(Reservation.plan_id == plan.plan_id).count() == 1
        assert session.query(MerchantOrder).filter(MerchantOrder.status == "scheduled").count() == 2
        assert session.query(Account).filter(Account.user_id == "user_001").one().available_minor == 400_000


def _receipt_for(database, plan_id: str) -> tuple[str, str]:
    with database.sessions() as session:
        plan = session.get(PlanRecord, plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan_id).one()
        assert plan is not None
        return receipt.id, plan.display_hash
