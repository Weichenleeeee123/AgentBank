"""SQLite schema and session factory used by all M0/M1 components."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from agentbank.clock import utc_now_naive


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


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


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    posted_at: Mapped[str] = mapped_column(String(32), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    merchant: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(64), default="其他")
    direction: Mapped[str] = mapped_column(String(16), default="debit")
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    risk_level: Mapped[str] = mapped_column(String(2))
    term_days: Mapped[int] = mapped_column(Integer, default=0)
    fee_bps: Mapped[int] = mapped_column(Integer, default=0)
    redemption_days: Mapped[int] = mapped_column(Integer, default=1)


class RiskProfile(Base):
    __tablename__ = "risk_profiles"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    level: Mapped[str] = mapped_column(String(2))
    score: Mapped[int] = mapped_column(Integer)
    expires_on: Mapped[str] = mapped_column(String(10))


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    limit_minor: Mapped[int] = mapped_column(Integer, default=100_000)
    restrictions_json: Mapped[str] = mapped_column(Text, default="[]")


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    merchant: Mapped[str] = mapped_column(String(160))
    amount_minor: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="active")
    next_charge_at: Mapped[str] = mapped_column(String(32))
    mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Mandate(Base):
    __tablename__ = "mandates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    plan_id: Mapped[str] = mapped_column(String(64))
    operation_type: Mapped[str] = mapped_column(String(120))
    subject_ref: Mapped[str] = mapped_column(String(120))
    max_amount_minor: Mapped[int] = mapped_column(Integer)
    max_uses: Mapped[int] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    starts_on: Mapped[str] = mapped_column(String(10))
    expires_on: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(32), default="active")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    event_date: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(160))


class WebImEvent(Base):
    __tablename__ = "webim_events"
    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="message")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class WebImNonce(Base):
    __tablename__ = "webim_nonces"
    nonce: Mapped[str] = mapped_column(String(160), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    record_id: Mapped[str] = mapped_column(String(80))
    display_hash: Mapped[str] = mapped_column(String(128))
    receipt_id: Mapped[str] = mapped_column(String(80), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class Reservation(Base):
    __tablename__ = "fund_reservations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="active")
    plan_id: Mapped[str] = mapped_column(String(64))


class CoolingPeriod(Base):
    __tablename__ = "cooling_periods"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    plan_id: Mapped[str] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="active")
    reconfirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SecurityCounter(Base):
    __tablename__ = "security_counters"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(String(120))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)


class HandoffCase(Base):
    __tablename__ = "handoff_cases"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    operation_type: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CapabilityRecord(Base):
    __tablename__ = "capability_records"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(8), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(48), default="created")
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    request_hash: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)


class ConversationState(Base):
    __tablename__ = "conversation_states"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    recent_user_messages_json: Mapped[str] = mapped_column(Text, default="[]")
    pending_intent_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_plan_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plan_revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)


class ConversationReference(Base):
    __tablename__ = "conversation_references"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    reference_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(8))
    slots_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class AABill(Base):
    __tablename__ = "aa_bills"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    total_minor: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class AAShare(Base):
    __tablename__ = "aa_shares"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    bill_id: Mapped[str] = mapped_column(String(80), index=True)
    participant_id: Mapped[str] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="requested")


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    schedule_id: Mapped[str] = mapped_column(String(80), index=True)
    due_date: Mapped[str] = mapped_column(String(10))
    period_index: Mapped[int] = mapped_column(Integer, default=1)
    operation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled")


class InvestmentOrder(Base):
    __tablename__ = "investment_orders"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    product_id: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(16))
    amount_minor: Mapped[int] = mapped_column(Integer)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))
    expected_settlement_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    product_id: Mapped[str] = mapped_column(String(80))
    shares: Mapped[int] = mapped_column(Integer)
    cost_minor: Mapped[int] = mapped_column(Integer)
    market_value_minor: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[datetime] = mapped_column(DateTime)


class CardApplication(Base):
    __tablename__ = "card_applications"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    card_id: Mapped[str] = mapped_column(String(80), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    card_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class DebitMandate(Base):
    __tablename__ = "debit_mandates"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    subscription_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")


class Reminder(Base):
    __tablename__ = "subscription_reminders"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    subscription_id: Mapped[str] = mapped_column(String(80))
    remind_before_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="scheduled")
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(120))
    payload_json: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    outbox_id: Mapped[str] = mapped_column(String(80), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class UserNotification(Base):
    __tablename__ = "user_notifications"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="unread")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class MerchantOrder(Base):
    __tablename__ = "merchant_orders"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    item: Mapped[str] = mapped_column(String(32))
    quote_id: Mapped[str] = mapped_column(String(80))
    amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    delivery_date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(32), default="scheduled")
    reservation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)


class MerchantCatalog(Base):
    __tablename__ = "merchant_catalog"
    item: Mapped[str] = mapped_column(String(32), primary_key=True)
    price_minor: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer, default=100)
    price_version: Mapped[int] = mapped_column(Integer, default=1)


class MerchantQuote(Base):
    __tablename__ = "merchant_quotes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    item: Mapped[str] = mapped_column(String(32))
    amount_minor: Mapped[int] = mapped_column(Integer)
    delivery_date: Mapped[str] = mapped_column(String(10))
    price_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class PlanRecord(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32))
    display_hash: Mapped[str] = mapped_column(String(128))
    action_hash: Mapped[str] = mapped_column(String(128))
    display_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


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
    delivery_code: Mapped[str | None] = mapped_column(String(6), nullable=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


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


class AuditCheckpoint(Base):
    __tablename__ = "audit_checkpoints"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, unique=True)
    event_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class PairingCode(Base):
    __tablename__ = "pairing_codes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    code_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class DemoRun(Base):
    __tablename__ = "demo_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="created")
    case_total: Mapped[int] = mapped_column(Integer, default=0)
    case_passed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class DailyRiskBucket(Base):
    __tablename__ = "daily_risk_buckets"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    business_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    completed_minor: Mapped[int] = mapped_column(Integer, default=0)
    pending_minor: Mapped[int] = mapped_column(Integer, default=0)


class _AtomicSession(Session):
    def close(self) -> None:
        active = self.info.get("atomic_session_active")
        if active is not None and active():
            return
        super().close()


class _SessionFactory:
    def __init__(self, factory: sessionmaker[_AtomicSession], active_session: ContextVar[Session | None]) -> None:
        self._factory = factory
        self._active_session = active_session

    def __call__(self) -> Session:
        return self._active_session.get() or self._factory()

    @contextmanager
    def begin(self) -> Iterator[Session]:
        active = self._active_session.get()
        if active is not None:
            yield active
            return
        with self._factory.begin() as session:
            yield session


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
        self._atomic_session: ContextVar[Session | None] = ContextVar(
            f"agentbank_atomic_session_{id(self)}", default=None
        )
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False,
                                             class_=_AtomicSession)
        self.sessions = _SessionFactory(self._session_factory, self._atomic_session)

    @contextmanager
    def atomic(self) -> Iterator[Session]:
        active = self._atomic_session.get()
        if active is not None:
            yield active
            return
        with self._session_factory() as session:
            session.info["atomic_session_active"] = lambda: self._atomic_session.get() is session
            token: Token[Session | None] = self._atomic_session.set(session)
            try:
                if self.engine.dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    session.begin()
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                self._atomic_session.reset(token)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "sqlite":
            columns = {column["name"] for column in inspect(self.engine).get_columns("capability_records")}
            if "request_hash" not in columns:
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(
                        "ALTER TABLE capability_records ADD COLUMN request_hash VARCHAR(128) NOT NULL DEFAULT ''"
                    )


def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
