from pathlib import Path

import pytest

from agentbank.analysis.dsl import DslExecutor
from agentbank.analysis.insights import detect_anomalies, detect_subscriptions, monthly_report
from agentbank.db import AuditLog, Database
from sqlalchemy import select
from agentbank.seed.loader import load_seed


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'analysis.db'}")
    load_seed(database)
    return database


def test_dsl_is_user_scoped_and_returns_traceable_transaction_ids(database: Database) -> None:
    result = DslExecutor(database).run("user_001", {
        "source": "transactions",
        "filters": [{"field": "category", "op": "eq", "value": "餐饮"}],
        "group_by": ["month"],
        "metrics": [{"fn": "sum", "field": "amount_minor"}, {"fn": "count"}],
        "limit": 100,
    })
    assert result.rows
    assert all(row["user_id"] == "user_001" for row in result.rows)
    assert result.query_hash.startswith("sha256:")
    assert all("txn_ids" in row for row in result.rows)
    with database.sessions() as session:
        event = session.scalar(select(AuditLog).where(AuditLog.event_type == "analysis.dsl.executed"))
        assert event is not None


def test_dsl_rejects_unknown_fields_and_large_limits(database: Database) -> None:
    with pytest.raises(ValueError, match="DSL_FIELD_NOT_ALLOWED"):
        DslExecutor(database).run("user_001", {"source": "transactions", "filters": [{"field": "password", "op": "eq", "value": "x"}]})
    with pytest.raises(ValueError, match="DSL_LIMIT_EXCEEDED"):
        DslExecutor(database).run("user_001", {"source": "transactions", "limit": 50_001})


def test_monthly_report_marks_missing_months_and_separates_transfer(database: Database) -> None:
    report = monthly_report(database, "user_001", "2026-05", "2026-07")
    assert [row["month"] for row in report] == ["2026-05", "2026-06", "2026-07"]
    assert all("missing" in row for row in report)
    assert all("income_minor" in row and "expense_minor" in row and "transfer_minor" in row for row in report)


def test_seed_contains_confirmed_and_suspected_subscriptions_and_anomalies(database: Database) -> None:
    subscriptions = detect_subscriptions(database, "user_001")
    assert any(item["status"] == "confirmed" for item in subscriptions)
    assert any(item["status"] == "suspected" for item in subscriptions)
    anomalies = detect_anomalies(database, "user_001")
    assert any(item["reason_code"] == "DUP_CHARGE" for item in anomalies)
