from sqlalchemy import inspect

from agentbank.db import Database


def test_m0_schema_contains_execution_and_audit_tables() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()

    tables = set(inspect(database.engine).get_table_names())

    assert {"users", "accounts", "plans", "receipts", "operations", "ledger_entries", "audit_log"} <= tables


def test_schema_upgrade_adds_capability_request_hash_to_existing_sqlite(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE capability_records ("
            "id VARCHAR(64) PRIMARY KEY, capability_id VARCHAR(16), user_id VARCHAR(64), "
            "status VARCHAR(48), data_json TEXT, idempotency_key VARCHAR(160), "
            "created_at DATETIME, updated_at DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO capability_records (id, capability_id, user_id, idempotency_key) "
            "VALUES ('legacy', 'Q01', 'user_001', 'legacy-key')"
        )

    database.create_schema()

    with database.engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT request_hash FROM capability_records WHERE id = 'legacy'"
        ).one()
    assert row.request_hash == ""
