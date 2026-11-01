"""Shared money invariants. Call only inside the database's atomic transaction."""
from datetime import timedelta
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentbank.clock import utc_now_naive
from agentbank.db import Account, DailyRiskBucket, Database, LedgerEntry, Operation, RiskContextRecord, Transaction
from agentbank.errors import DomainError
from agentbank.security.breaker import CircuitBreaker


def require_active(account: Account | None, user_id: str) -> Account:
    if account is None or account.user_id != user_id:
        raise DomainError('NOT_OWNER')
    if account.status != 'active':
        raise DomainError('ACCOUNT_NOT_ACTIVE')
    return account


def bucket_for(session: Session, user_id: str, day: str) -> DailyRiskBucket:
    bucket = session.get(DailyRiskBucket, (user_id, day))
    if bucket is None:
        bucket = DailyRiskBucket(user_id=user_id, business_date=day, completed_minor=0, pending_minor=0)
        session.add(bucket)
        session.flush()
    return bucket


def recent_transfers(session: Session, user_id: str, recipient_id: str) -> int:
    return len(session.scalars(select(Operation.id).where(
        Operation.user_id == user_id, Operation.recipient_id == recipient_id,
        Operation.status == 'succeeded', Operation.created_at >= utc_now_naive() - timedelta(minutes=10),
    )).all())


def require_safe_risk(session: Session, user_id: str) -> None:
    risk = session.get(RiskContextRecord, user_id)
    if risk is not None and risk.expires_at > utc_now_naive():
        if risk.level == 'high':
            raise DomainError('FRAUD_REVIEW_REQUIRED')
        if risk.level in {'medium', 'unknown'}:
            raise DomainError('RISK_REVIEW_REQUIRED')


def require_payment(database: Database, session: Session, user_id: str, account: Account | None,
                    amount: int, day: str, *, mfa: bool = False, held: int = 0,
                    recipient_id: str | None = None) -> DailyRiskBucket:
    account = require_active(account, user_id)
    CircuitBreaker(database).require_write(session, user_id)
    if amount <= 0 or held < 0 or held > amount:
        raise DomainError('INVALID_AMOUNT')
    if account.available_minor < amount - held:
        raise DomainError('INSUFFICIENT_FUNDS')
    require_safe_risk(session, user_id)
    bucket = bucket_for(session, user_id, day)
    if not mfa:
        if bucket.completed_minor + bucket.pending_minor + amount > 100000:
            raise DomainError('DAILY_TOTAL_OVER_1000')
        if recipient_id and recent_transfers(session, user_id, recipient_id) >= 2:
            raise DomainError('MFA_REQUIRED')
    return bucket


def post_payment(session: Session, operation_id: str, account: Account, destination: str,
                 amount: int, day: str, *, category: str, merchant: str,
                 direction: str = 'transfer', held: int = 0) -> list[str]:
    """Debit cash, credit local counterparty if present, and write both journal views."""
    existing = session.scalars(select(LedgerEntry).where(LedgerEntry.operation_id == operation_id)).all()
    if existing:
        return [entry.id for entry in existing]
    if amount <= 0 or held < 0 or held > amount:
        raise DomainError('INVALID_AMOUNT')
    require_active(account, account.user_id)
    if account.available_minor < amount - held:
        raise DomainError('INSUFFICIENT_FUNDS')
    recipient = session.get(Account, destination)
    if recipient is not None:
        require_active(recipient, recipient.user_id)
    account.available_minor -= amount - held
    account.ledger_balance_minor -= amount
    if recipient is not None:
        recipient.available_minor += amount
        recipient.ledger_balance_minor += amount
    ids = [f'le_{uuid.uuid4().hex}', f'le_{uuid.uuid4().hex}']
    session.add_all([
        LedgerEntry(id=ids[0], operation_id=operation_id, account_id=account.id, amount_minor=-amount, direction='debit'),
        LedgerEntry(id=ids[1], operation_id=operation_id, account_id=destination, amount_minor=amount, direction='credit'),
        Transaction(id=f'txn_{operation_id}_out', user_id=account.user_id, account_id=account.id,
                    posted_at=day, amount_minor=amount, merchant=merchant, category=category,
                    direction=direction, memo=f'操作 {operation_id}'),
    ])
    if recipient is not None and recipient.id != account.id:
        session.add(Transaction(id=f'txn_{operation_id}_in', user_id=recipient.user_id,
                    account_id=recipient.id, posted_at=day, amount_minor=amount, merchant=merchant,
                    category=category, direction='transfer', memo=f'操作 {operation_id}'))
    return ids


def post_redemption(session: Session, operation_id: str, account: Account, source: str,
                    amount: int, day: str) -> None:
    if session.scalar(select(LedgerEntry.id).where(LedgerEntry.operation_id == operation_id)):
        return
    require_active(account, account.user_id)
    account.available_minor += amount
    account.ledger_balance_minor += amount
    session.add_all([
        LedgerEntry(id=f'le_{uuid.uuid4().hex}', operation_id=operation_id, account_id=source, amount_minor=-amount, direction='debit'),
        LedgerEntry(id=f'le_{uuid.uuid4().hex}', operation_id=operation_id, account_id=account.id, amount_minor=amount, direction='credit'),
        Transaction(id=f'txn_{operation_id}_in', user_id=account.user_id, account_id=account.id,
                    posted_at=day, amount_minor=amount, merchant='理财赎回', category='理财',
                    direction='transfer', memo=f'操作 {operation_id}'),
    ])
