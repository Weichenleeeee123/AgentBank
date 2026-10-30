from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Account, DailyRiskBucket, Database, Mandate, Operation, ScheduledJob
from agentbank.seed.loader import load_seed


def _service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'schedule-atomic.db'}")
    load_seed(database)
    return CapabilityService(database)


def _scheduled(service: CapabilityService, key: str = "schedule-atomic"):
    return service.execute("user_001", "T04", {
        "recipient_name": "房东", "amount_minor": 1_000,
        "next_due": service.clock.business_date(), "max_uses": 1,
    }, key)


def test_concurrent_dispatches_consume_one_mandate_and_create_one_operation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = _scheduled(service)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.dispatch_due, "user_001") for _ in range(2)]
        outcomes = [future.result() for future in futures]

    with service.database.sessions() as session:
        mandate = session.get(Mandate, result.data["mandate_id"])
        operations = list(session.scalars(select(Operation).where(
            Operation.plan_id == result.data["schedule_id"]
        )))
        jobs = list(session.scalars(select(ScheduledJob).where(
            ScheduledJob.schedule_id == result.data["schedule_id"]
        )))
    assert mandate.used_count == 1
    assert len(operations) == 1
    assert sum(job.status == "succeeded" for job in jobs) == 1
    assert sum(len(items) for items in outcomes) == 1


def test_insufficient_funds_pauses_without_consuming_mandate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    scheduled = _scheduled(service)
    with service.database.sessions.begin() as session:
        account = session.scalar(select(Account).where(Account.user_id == "user_001"))
        account.available_minor = 0

    result = service.dispatch_due("user_001")[0]

    with service.database.sessions() as session:
        mandate = session.get(Mandate, scheduled.data["mandate_id"])
    assert result.status == "paused"
    assert result.data["error_code"] == "INSUFFICIENT_FUNDS"
    assert mandate.used_count == 0


def test_revoked_mandate_pauses_without_spending_or_consuming(tmp_path: Path) -> None:
    service = _service(tmp_path)
    scheduled = _scheduled(service)
    service.revoke_mandate("user_001", scheduled.data["mandate_id"])

    result = service.dispatch_due("user_001")[0]

    with service.database.sessions() as session:
        mandate = session.get(Mandate, scheduled.data["mandate_id"])
        assert session.scalar(select(Operation).where(Operation.plan_id == scheduled.data["schedule_id"])) is None
        account = session.scalar(select(Account).where(Account.user_id == "user_001"))
    assert result.status == "paused"
    assert result.data["error_code"] == "MANDATE_INVALID"
    assert mandate.used_count == 0
    assert account.available_minor == 500_000


def test_expired_mandate_pauses_without_consuming(tmp_path: Path) -> None:
    service = _service(tmp_path)
    scheduled = _scheduled(service)
    with service.database.sessions.begin() as session:
        mandate = session.get(Mandate, scheduled.data["mandate_id"])
        mandate.expires_on = "2000-01-01"

    result = service.dispatch_due("user_001")[0]

    with service.database.sessions() as session:
        mandate = session.get(Mandate, scheduled.data["mandate_id"])
    assert result.status == "paused"
    assert result.data["error_code"] == "MANDATE_INVALID"
    assert mandate.used_count == 0


def test_new_payee_pauses_scheduled_transfer_without_consumption(tmp_path: Path) -> None:
    service = _service(tmp_path)
    scheduled = _scheduled(service)
    with service.database.sessions.begin() as session:
        from agentbank.db import Payee

        payee = session.get(Payee, scheduled.data["recipient_id"])
        payee.is_new = True

    result = service.dispatch_due("user_001")[0]

    with service.database.sessions() as session:
        mandate = session.get(Mandate, scheduled.data["mandate_id"])
        account = session.scalar(select(Account).where(Account.user_id == "user_001"))
    assert result.status == "paused"
    assert result.data["error_code"] == "RECIPIENT_RISK_UPGRADE"
    assert mandate.used_count == 0
    assert account.available_minor == 500_000


def test_daily_limit_pauses_without_consuming_mandate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    scheduled = _scheduled(service)
    today = service.clock.business_date()
    with service.database.sessions.begin() as session:
        session.add(DailyRiskBucket(user_id="user_001", business_date=today,
                                    completed_minor=100_000, pending_minor=0))

    result = service.dispatch_due("user_001")[0]

    with service.database.sessions() as session:
        mandate = session.get(Mandate, scheduled.data["mandate_id"])
        assert session.get(DailyRiskBucket, ("user_001", today)).completed_minor == 100_000
        assert session.scalar(select(Operation).where(Operation.plan_id == scheduled.data["schedule_id"])) is None
    assert result.status == "paused"
    assert result.data["error_code"] == "DAILY_TOTAL_OVER_1000"
    assert mandate.used_count == 0
