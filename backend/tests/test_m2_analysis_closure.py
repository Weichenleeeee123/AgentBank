from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from agentbank.analysis import dsl
from agentbank.analysis.dsl import DslExecutor
from agentbank.analysis.insights import monthly_report, yearly_report
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database, Transaction, User


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'analysis-closure.db'}")
    db.create_schema()
    return db


def _add(database: Database, txn_id: str, posted_at: str, amount: int, direction: str = "debit") -> None:
    with database.sessions.begin() as session:
        session.add(Transaction(id=txn_id, user_id="u1", posted_at=posted_at, amount_minor=amount,
                                merchant="shop", category="food", direction=direction))


def test_file_sqlite_analysis_connection_rejects_writes(database: Database) -> None:
    executor = DslExecutor(database)
    with executor._readonly_session() as session:
        with pytest.raises(OperationalError, match="readonly"):
            session.connection().exec_driver_sql(
                "INSERT INTO transactions (id, user_id, posted_at, amount_minor, merchant, category, direction) "
                "VALUES ('bad', 'u1', '2026-01-01', 1, 'x', 'x', 'debit')"
            )


def test_dsl_progress_handler_interrupts_query_at_deadline(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "WITH RECURSIVE rows(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM rows WHERE n < 50000) "
            "INSERT INTO transactions (id, user_id, posted_at, amount_minor, merchant, category, direction) "
            "SELECT 'timeout-' || n, 'u1', '2026-01-01', 1, 'x', 'x', 'debit' FROM rows"
        )
    ticks = iter([0.0, 3.0])
    monkeypatch.setattr(dsl, "perf_counter", lambda: next(ticks, 3.0))

    with pytest.raises(ValueError, match="DSL_TIMEOUT"):
        DslExecutor(database).run("u1", {"source": "transactions"})


def test_dsl_treats_user_id_as_a_parameter(database: Database) -> None:
    _add(database, "one", "2026-01-01", 1)

    result = DslExecutor(database).run("u1' OR 1=1 --", {"source": "transactions"})

    assert result.rows == []


def test_dsl_deadline_includes_python_materialization(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _add(database, "slow-row", "2026-01-01", 100)
    original = DslExecutor._record_row
    ticks = [0.0]

    def slow_row(record):
        ticks[0] = 3.0
        return original(record)

    monkeypatch.setattr(dsl, "perf_counter", lambda: ticks[0])
    monkeypatch.setattr(DslExecutor, "_record_row", staticmethod(slow_row))
    with pytest.raises(ValueError, match="DSL_TIMEOUT"):
        DslExecutor(database).run("u1", {"source": "transactions"})


def test_report_period_is_banking_timezone_and_range_bounded(database: Database) -> None:
    _add(database, "utc-boundary", "2026-01-31T16:30:00+00:00", 123)
    rows = monthly_report(database, "u1", "2026-01", "2026-02")
    assert rows[0]["expense_minor"] == 0
    assert rows[1]["expense_minor"] == 123
    with pytest.raises(ValueError, match="REPORT_RANGE_INVALID"):
        monthly_report(database, "u1", "2026-12", "2026-01")
    with pytest.raises(ValueError, match="REPORT_RANGE_INVALID"):
        monthly_report(database, "u1", "1900-01", "9999-12")


def test_transaction_end_date_includes_timestamp_rows_on_that_date(database: Database) -> None:
    _add(database, "at-end-date", "2026-01-31T23:59:59.999999", 123)
    result = CapabilityService(database).execute("u1", "Q02", {
        "start_date": "2026-01-31", "end_date": "2026-01-31", "limit": 10,
    }, "inclusive-end-date")
    assert [row["txn_id"] for row in result.data["transactions"]] == ["at-end-date"]


def test_month_report_uses_out_of_range_bases_and_reconcilable_evidence(database: Database) -> None:
    _add(database, "prior-year", "2025-01-31", 100)
    _add(database, "prior-month", "2025-12-31", 200)
    _add(database, "jan-expense", "2026-01-01", 300)
    _add(database, "jan-income", "2026-01-15", 900, "credit")
    _add(database, "jan-transfer", "2026-01-20", 700, "transfer")

    row = monthly_report(database, "u1", "2026-01", "2026-01")[0]

    assert row["month"] == "2026-01"
    assert row["expense_minor"] == 300
    assert row["income_minor"] == 900
    assert row["transfer_minor"] == 700
    assert row["mom_change"] == 0.5
    assert row["yoy_change"] == 2.0
    assert set(row["txn_ids"]) == {"jan-expense", "jan-income", "jan-transfer"}
    assert row["expense_txn_ids"] == ["jan-expense"]
    assert row["income_txn_ids"] == ["jan-income"]
    assert row["transfer_txn_ids"] == ["jan-transfer"]
    with database.sessions() as session:
        evidence = list(session.scalars(select(Transaction).where(Transaction.id.in_(row["txn_ids"]))))
    by_id = {item.id: item for item in evidence}
    for direction, id_key, amount_key in (
        ("debit", "expense_txn_ids", "expense_minor"),
        ("credit", "income_txn_ids", "income_minor"),
        ("transfer", "transfer_txn_ids", "transfer_minor"),
    ):
        ids = row[id_key]
        assert sum(by_id[txn_id].amount_minor for txn_id in ids) == row[amount_key]
        assert all(by_id[txn_id].direction == direction for txn_id in ids)


def test_month_and_year_reports_cover_calendar_periods_and_missing_bases(database: Database) -> None:
    _add(database, "dec", "2024-12-31", 100)
    _add(database, "feb", "2025-02-01", 150)

    months = monthly_report(database, "u1", "2025-01", "2025-02")
    years = yearly_report(database, "u1", 2023, 2026)

    assert [row["month"] for row in months] == ["2025-01", "2025-02"]
    assert months[0]["missing"] is True
    assert months[0]["mom_change"] is None
    assert months[0]["yoy_change"] is None
    assert months[1]["expense_minor"] == 150
    assert months[1]["mom_change"] is None
    assert [row["year"] for row in years] == [2023, 2024, 2025, 2026]
    assert years[0]["missing"] is True
    assert years[0]["yoy_change"] is None
    assert years[1]["expense_minor"] == 100
    assert years[1]["yoy_change"] is None
    assert years[2]["expense_minor"] == 150
    assert years[2]["yoy_change"] is None
    assert years[2]["missing_months"] == [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert years[3]["missing"] is True
    assert years[3]["yoy_change"] is None


def test_b04_uses_annual_aggregates_and_evidence_with_legacy_date_payload(database: Database) -> None:
    _add(database, "baseline", "2025-01-31", 100)
    _add(database, "current-jan", "2026-01-01", 200)
    _add(database, "current-dec", "2026-12-31", 100)
    _add(database, "transfer", "2026-10-30", 900, "transfer")
    service = CapabilityService(database)

    result = service.execute("u1", "B04", {"year": "2026"}, "annual-by-year")
    legacy = service.execute("u1", "B04", {
        "start_month": "2026-05", "end_month": "2026-07",
    }, "annual-by-month-range")

    assert len(result.data["months"]) == 12
    assert result.data["years"] == legacy.data["years"]
    annual = result.data["years"][0]
    assert annual["year"] == 2026
    assert annual["expense_minor"] == 300
    assert annual["transfer_minor"] == 900
    assert annual["yoy_change"] is None
    assert annual["complete"] is False
    assert set(annual["txn_ids"]) == {"current-jan", "current-dec", "transfer"}


def test_report_http_capabilities_return_exact_month_changes_and_yearly_totals(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from agentbank.api.app import create_app

    app = create_app(f"sqlite:///{tmp_path / 'report-http.db'}")
    database = app.state.database
    with database.sessions.begin() as session:
        session.add(User(id="u1", display_name="Test User"))
    _add(database, "yoy-base", "2025-01-31", 100)
    _add(database, "mom-base", "2025-12-31", 200)
    _add(database, "current", "2026-01-01", 300)
    client = TestClient(app)
    token = client.post("/v1/sessions", json={"user_id": "u1"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    monthly = client.post("/v1/capabilities/B03", headers=headers, json={
        "capability_id": "B03", "idempotency_key": "http-month-changes",
        "payload": {"start_month": "2026-01", "end_month": "2026-02"},
    })
    annual = client.post("/v1/capabilities/B04", headers=headers, json={
        "capability_id": "B04", "idempotency_key": "http-year-totals",
        "payload": {"year": "2026"},
    })

    assert monthly.status_code == annual.status_code == 200
    months = monthly.json()["data"]["months"]
    assert [row["month"] for row in months] == ["2026-01", "2026-02"]
    assert months[0]["mom_change"] == 0.5
    assert months[0]["yoy_change"] == 2.0
    assert months[1]["missing"] is True
    data = annual.json()["data"]
    assert len(data["months"]) == 12
    assert data["years"][0]["expense_minor"] == 300
    assert data["years"][0]["txn_ids"] == ["current"]


def test_complete_years_keep_meaningful_yoy(database: Database) -> None:
    for month in range(1, 13):
        _add(database, f"base-{month}", f"2024-{month:02d}-01", 100)
        _add(database, f"next-{month}", f"2025-{month:02d}-01", 150)
    row = yearly_report(database, "u1", 2025, 2025)[0]
    assert row["complete"] and row["missing_months"] == []
    assert row["yoy_change"] == 0.5
