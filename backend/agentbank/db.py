"""SQLite schema and session factory used by all M0/M1 components."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    available_minor: Mapped[int] = mapped_column(Integer, default=0)
    ledger_balance_minor: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="active")


class Payee(Base):
    __tablename__ = "payees"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    masked_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_id: Mapped[str] = mapped_column(String(64))
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)


class PlanRecord(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32))
    display_hash: Mapped[str] = mapped_column(String(128))
    action_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlanNodeRecord(Base):
    __tablename__ = "plan_nodes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(64), index=True)
    tool: Mapped[str] = mapped_column(String(120))
    args_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)


class DecisionRecord(Base):
    __tablename__ = "decisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(64), index=True)
    operation_id: Mapped[str] = mapped_column(String(64))
    tier_display: Mapped[str] = mapped_column(String(1))
    assurance: Mapped[str] = mapped_column(String(16))
    action_hash: Mapped[str] = mapped_column(String(128))
    reason_codes_json: Mapped[str] = mapped_column(Text)
    facts_json: Mapped[str] = mapped_column(Text)


class ReceiptRecord(Base):
    __tablename__ = "receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    plan_id: Mapped[str] = mapped_column(String(64))
    plan_revision: Mapped[int] = mapped_column(Integer)
    node_ids_json: Mapped[str] = mapped_column(Text)
    action_hash: Mapped[str] = mapped_column(String(128))
    display_hash: Mapped[str] = mapped_column(String(128))
    assurance: Mapped[str] = mapped_column(String(16))
    issued_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="pending")


class ReceiptConsumption(Base):
    __tablename__ = "receipt_consumptions"
    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(64))
    consumed_at: Mapped[datetime] = mapped_column(DateTime)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    paired_at: Mapped[datetime] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Challenge(Base):
    __tablename__ = "challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    receipt_id: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    plan_id: Mapped[str] = mapped_column(String(64))
    node_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    amount_minor: Mapped[int] = mapped_column(Integer)
    recipient_id: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(120))
    payload_json: Mapped[str] = mapped_column(Text)
    previous_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)


class DailyRiskBucket(Base):
    __tablename__ = "daily_risk_buckets"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    business_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    completed_minor: Mapped[int] = mapped_column(Integer, default=0)
    pending_minor: Mapped[int] = mapped_column(Integer, default=0)


class Database:
    def __init__(self, url: str = "sqlite:///agentbank.db") -> None:
        connect_args: dict[str, Any] = {}
        kwargs: dict[str, Any] = {}
        if url in {"sqlite://", "sqlite:///:memory:"}:
            connect_args["check_same_thread"] = False
            kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, connect_args=connect_args, **kwargs)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", _configure_sqlite)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)


def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
