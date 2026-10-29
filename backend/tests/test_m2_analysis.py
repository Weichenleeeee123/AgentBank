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
    with pytest.raises(ValueError, match="DSL_METRIC_NOT_ALLOWED"):
        DslExecutor(database).run("user_001", {"source": "transactions", "metrics": [{"fn": "raw_sql", "field": "amount_minor"}]})
    with pytest.raises(ValueError, match="DSL_FIELD_NOT_ALLOWED"):
        DslExecutor(database).run("user_001", {"source": "transactions", "user_id": "user_002"})


def test_dsl_applies_requested_metrics_and_ordering(database: Database) -> None:
    result = DslExecutor(database).run("user_001", {
        "source": "transactions",
        "group_by": ["category"],
        "metrics": [{"fn": "count"}],
        "order_by": [{"field": "count", "dir": "desc"}],
        "limit": 10,
    })
    counts = [row["count"] for row in result.rows]
    assert counts == sorted(counts, reverse=True)
    assert all("count" in row and "sum_amount_minor" not in row and row["txn_ids"] for row in result.rows)


def test_dsl_rejects_silent_truncation_above_hard_row_limit(database: Database) -> None:
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "WITH RECURSIVE rows(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM rows WHERE n < 50001) "
            "INSERT INTO transactions (id, user_id, posted_at, amount_minor, merchant, category, direction) "
            "SELECT 'cap-' || n, 'user_001', '2026-10-01', 1, 'limit-test', 'test', 'debit' FROM rows"
        )
    with pytest.raises(ValueError, match="DSL_ROW_LIMIT_EXCEEDED"):
        DslExecutor(database).run("user_001", {"source": "transactions", "group_by": ["category"], "limit": 1})


def test_monthly_report_marks_missing_months_and_separates_transfer(database: Database) -> None:
    report = monthly_report(database, "user_001", "2026-05", "2026-07")
    assert [row["month"] for row in report] == ["2026-05", "2026-06", "2026-07"]
    assert all("missing" in row for row in report)
    assert all("income_minor" in row and "expense_minor" in row and "transfer_minor" in row for row in report)
    assert all("mom_change" in row and "yoy_change" in row for row in report)


def test_seed_contains_confirmed_and_suspected_subscriptions_and_anomalies(database: Database) -> None:
    subscriptions = detect_subscriptions(database, "user_001")
    assert any(item["status"] == "confirmed" for item in subscriptions)
    assert any(item["status"] == "suspected" for item in subscriptions)
    anomalies = detect_anomalies(database, "user_001")
    assert any(item["reason_code"] == "DUP_CHARGE" for item in anomalies)
    assert any(item["reason_code"] == "AMOUNT_OUTLIER" for item in anomalies)
    assert any(item["reason_code"] == "PROBE_THEN_LARGE" for item in anomalies)
    assert any(item["reason_code"] == "NEW_MERCHANT_LARGE" for item in anomalies)
