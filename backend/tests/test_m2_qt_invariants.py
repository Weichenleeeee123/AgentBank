from pathlib import Path

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Account, Database, LedgerEntry, Operation, ScheduledJob
from agentbank.seed.loader import load_seed


def make_service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'qt.db'}")
    load_seed(database)
    return CapabilityService(database)


def test_q02_filters_are_applied_and_user_scoped(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    result = service.execute("user_001", "Q02", {"category": "餐饮", "limit": 2}, "q02-filter")
    assert len(result.data["transactions"]) <= 2
    assert all(item["category"] == "餐饮" for item in result.data["transactions"])
    assert all(item["user_id"] == "user_001" for item in result.data["transactions"])


def test_t01_confirmation_creates_balanced_debit_credit_and_operation(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    prepared = service.execute("user_001", "T01", {"recipient_name": "房东", "amount_minor": 2_000}, "qt-t01")
    confirmed = service.confirm_transfer("user_001", prepared.record_id, prepared.data["display_hash"])
    operation_id = confirmed.data["transfer_result"]["operation_id"]
    with service.database.sessions() as session:
        entries = session.query(LedgerEntry).filter(LedgerEntry.operation_id == operation_id).all()
        assert sum(entry.amount_minor for entry in entries) == 0
        assert session.get(Operation, operation_id).status == "succeeded"


def test_t04_runs_two_periods_and_deduplicates_dispatch(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    scheduled = service.execute("user_001", "T04", {"recipient_name": "房东", "amount_minor": 1_000,
                                                     "next_due": service.clock.business_date(), "max_uses": 2}, "qt-t04")
    first = service.dispatch_due("user_001")
    assert first and first[0].data["dispatched_count"] == 1
    duplicate = service.dispatch_due("user_001")
    assert duplicate == []
    service.clock.advance_days(30)
    second = service.dispatch_due("user_001")
    assert second and second[0].data["dispatched_count"] == 2
    assert second[0].data["operation_id"] != first[0].data["operation_id"]
    with service.database.sessions() as session:
        assert session.query(ScheduledJob).filter(ScheduledJob.schedule_id == scheduled.data["schedule_id"]).count() >= 2
        account = session.query(Account).filter(Account.user_id == "user_001").one()
        assert account.available_minor == 498_000
