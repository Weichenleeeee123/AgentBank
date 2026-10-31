from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from sqlalchemy import select

from agentbank.clock import utc_now_naive
from agentbank.db import Challenge, Database, Device, PairingCode, SecurityLock, ReceiptRecord
from agentbank.audit.store import PersistentAudit
from agentbank.errors import DomainError


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DeviceService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def register(self, user_id: str) -> str:
        raw = secrets.token_urlsafe(32)
        with self.database.sessions.begin() as session:
            session.add(Device(id=f"dev_{uuid.uuid4().hex}", user_id=user_id, token_hash=_hash(raw), paired_at=utc_now_naive()))
        return raw

    def create_pairing_code(self, user_id: str, ttl_minutes: int = 10) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self.database.sessions.begin() as session:
            session.add(PairingCode(id=f"pair_{uuid.uuid4().hex}", user_id=user_id,
                                    code_hash=_hash(code),
                                    expires_at=utc_now_naive() + timedelta(minutes=ttl_minutes)))
        return code

    def redeem_pairing_code(self, code: str) -> str:
        with self.database.atomic() as session:
            pairing = session.scalar(select(PairingCode).where(PairingCode.code_hash == _hash(code),
                                                               PairingCode.consumed.is_(False)))
            if pairing is None or pairing.expires_at <= utc_now_naive():
                raise DomainError("PAIRING_CODE_INVALID")
            pairing.consumed = True
            raw = secrets.token_urlsafe(32)
            session.add(Device(id=f"dev_{uuid.uuid4().hex}", user_id=pairing.user_id,
                               token_hash=_hash(raw), paired_at=utc_now_naive()))
            PersistentAudit(self.database).append(session, f"trace_{pairing.id}", pairing.user_id,
                                                  "device.paired", {"pairing_id": pairing.id})
            return raw

    def issue_otp(self, user_id: str, receipt_id: str, action_hash: str = "") -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self.database.atomic() as session:
            receipt = session.get(ReceiptRecord, receipt_id)
            if receipt is not None:
                if receipt.subject_id != user_id or receipt.status != "pending":
                    raise DomainError("RECEIPT_INVALID")
                if action_hash and action_hash != receipt.action_hash:
                    raise DomainError("OTP_ACTION_MISMATCH")
                action_hash = receipt.action_hash
            session.add(Challenge(id=f"ch_{uuid.uuid4().hex}", user_id=user_id, receipt_id=receipt_id,
                                  action_hash=action_hash, code_hash=_hash(code), delivery_code=code,
                                  expires_at=utc_now_naive() + timedelta(minutes=5)))
        return code

    def inbox(self, device_token: str) -> list[dict[str, str]]:
        with self.database.sessions() as session:
            device = session.scalar(select(Device).where(Device.token_hash == _hash(device_token), Device.active.is_(True)))
            if device is None:
                raise DomainError("DEVICE_UNAUTHORIZED")
            challenges = session.scalars(select(Challenge).where(Challenge.user_id == device.user_id,
                                                                  Challenge.consumed.is_(False),
                                                                  Challenge.expires_at > utc_now_naive()).order_by(Challenge.expires_at.desc())).all()
            return [{"challenge_id": item.id, "receipt_id": item.receipt_id, "code": item.delivery_code or ""} for item in challenges]

    def validate_token(self, user_id: str, device_token: str) -> None:
        with self.database.sessions() as session:
            device = session.scalar(select(Device).where(Device.user_id == user_id,
                                                          Device.token_hash == _hash(device_token),
                                                          Device.active.is_(True)))
            if device is None:
                raise DomainError("DEVICE_UNAUTHORIZED")

    def user_for_token(self, device_token: str) -> str:
        with self.database.sessions() as session:
            device = session.scalar(select(Device).where(Device.token_hash == _hash(device_token), Device.active.is_(True)))
            if device is None:
                raise DomainError("DEVICE_UNAUTHORIZED")
            return device.user_id

    def is_user_locked(self, user_id: str, now: datetime | None = None) -> bool:
        current = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
        with self.database.sessions() as session:
            lock = session.get(SecurityLock, user_id)
            return lock is not None and lock.locked_until > current

    def record_otp_failure(self, user_id: str, reason: str = "otp_failure",
                           now: datetime | None = None) -> bool:
        current = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
        with self.database.atomic() as session:
            return self._record_otp_failure_in_session(session, user_id, reason, current)

    def verify(self, user_id: str, code: str, receipt_id: str | None = None,
               action_hash: str | None = None) -> Challenge:
        failure: str | None = None
        verified: Challenge | None = None
        with self.database.atomic() as session:
            current = utc_now_naive()
            lock = session.get(SecurityLock, user_id)
            if lock is not None and lock.locked_until > current:
                raise DomainError("OTP_LOCKED")
            query = select(Challenge).where(Challenge.user_id == user_id, Challenge.consumed.is_(False))
            if receipt_id is not None:
                query = query.where(Challenge.receipt_id == receipt_id)
            challenge = session.scalar(query.order_by(Challenge.expires_at.desc()))
            if challenge is None or challenge.expires_at <= current:
                raise DomainError("OTP_EXPIRED")
            if action_hash is not None and challenge.action_hash and challenge.action_hash != action_hash:
                raise DomainError("OTP_ACTION_MISMATCH")
            if challenge.attempts >= 3:
                self._record_otp_failure_in_session(session, user_id, "challenge_attempt_limit", current)
                failure = "OTP_LOCKED"
            else:
                challenge.attempts += 1
                if not secrets.compare_digest(challenge.code_hash, _hash(code)):
                    self._record_otp_failure_in_session(session, user_id, "otp_failure", current)
                    failure = "OTP_INVALID"
                else:
                    challenge.consumed = True
                    challenge.delivery_code = None
                    if lock is not None:
                        lock.failed_attempts = 0
                        lock.first_failed_at = current
                    session.flush()
                    verified = challenge
        if failure is not None:
            raise DomainError(failure)
        if verified is None:
            raise DomainError("OTP_INVALID")
        return verified

    @staticmethod
    def _record_otp_failure_in_session(session: Session, user_id: str, reason: str,
                                        current: datetime) -> bool:
        lock = session.get(SecurityLock, user_id)
        window_start = current - timedelta(minutes=10)
        if lock is None:
            lock = SecurityLock(user_id=user_id, locked_until=current,
                                failed_attempts=0, first_failed_at=current, reason=reason)
            session.add(lock)
        elif lock.first_failed_at < window_start:
            lock.first_failed_at = current
            lock.failed_attempts = 0
            lock.locked_until = current
        lock.failed_attempts += 1
        lock.reason = reason
        if lock.failed_attempts >= 3:
            lock.locked_until = current + timedelta(minutes=15)
        return lock.locked_until > current
