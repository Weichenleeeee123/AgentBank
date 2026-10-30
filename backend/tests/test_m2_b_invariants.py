from pathlib import Path

from agentbank.analysis.dsl import DslExecutor
from agentbank.analysis.insights import detect_anomalies, monthly_report
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database
from agentbank.seed.loader import load_seed


def make_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'b.db'}")
    load_seed(database)
    return database


def test_b01_groups_reconcile_to_transaction_ids(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    result = DslExecutor(database).run("user_001", {"source": "transactions", "group_by": ["category"], "limit": 100})
    total_ids = sum(len(row["txn_ids"]) for row in result.rows)
    with database.sessions() as session:
        from agentbank.db import Transaction
        total_rows = session.query(Transaction).filter(Transaction.user_id == "user_001").count()
    assert total_ids == total_rows
    assert all(row["txn_ids"] for row in result.rows)


def test_b02_each_anomaly_has_reason_and_transaction_evidence(tmp_path: Path) -> None:
    anomalies = detect_anomalies(make_database(tmp_path), "user_001")
    assert anomalies
    assert all(item["reason_code"] and item["txn_ids"] and item["evidence"] for item in anomalies)


def test_b03_month_report_separates_flows(tmp_path: Path) -> None:
    rows = monthly_report(make_database(tmp_path), "user_001", "2026-05", "2026-07")
    assert len(rows) == 3
    assert any(row["income_minor"] > 0 for row in rows)
    assert all(set(("income_minor", "expense_minor", "transfer_minor", "missing")) <= set(row) for row in rows)


def test_b04_year_report_returns_calendar_year_aggregate(tmp_path: Path) -> None:
    result = CapabilityService(make_database(tmp_path)).execute(
        "user_001", "B04", {"year": "2026"}, "b04-year-report"
    )
    assert len(result.data["months"]) == 12
    assert len(result.data["years"]) == 1
    assert result.data["years"][0]["year"] == 2026
    assert "yoy_change" in result.data["years"][0]
