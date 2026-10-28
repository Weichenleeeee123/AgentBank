from sqlalchemy import inspect

from agentbank.db import Database


def test_m0_schema_contains_execution_and_audit_tables() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()

    tables = set(inspect(database.engine).get_table_names())

    assert {"users", "accounts", "plans", "receipts", "operations", "ledger_entries", "audit_log"} <= tables
