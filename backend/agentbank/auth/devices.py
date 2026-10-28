from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select

from agentbank.clock import utc_now_naive
from agentbank.db import Challenge, Database, Device
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

    def issue_otp(self, user_id: str, receipt_id: str) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self.database.sessions.begin() as session:
            session.add(Challenge(id=f"ch_{uuid.uuid4().hex}", user_id=user_id, receipt_id=receipt_id,
                                  code_hash=_hash(code), delivery_code=code,
                                  expires_at=utc_now_naive() + timedelta(minutes=5)))
        return code

    def inbox(self, device_token: str) -> list[dict[str, str]]:
        with self.database.sessions() as session:
            device = session.scalar(select(Device).where(Device.token_hash == _hash(device_token), Device.active.is_(True)))
            if device is None:
                raise DomainError("DEVICE_UNAUTHORIZED")
            challenges = session.scalars(select(Challenge).where(Challenge.user_id == device.user_id,
                                                                  Challenge.consumed.is_(False),
                                                                  Challenge.expires_at > utc_now_naive())).all()
            return [{"challenge_id": item.id, "receipt_id": item.receipt_id, "code": item.delivery_code or ""} for item in challenges]

    def validate_token(self, user_id: str, device_token: str) -> None:
        with self.database.sessions() as session:
            device = session.scalar(select(Device).where(Device.user_id == user_id,
                                                          Device.token_hash == _hash(device_token),
                                                          Device.active.is_(True)))
            if device is None:
                raise DomainError("DEVICE_UNAUTHORIZED")

    def verify(self, user_id: str, code: str) -> Challenge:
        with self.database.sessions.begin() as session:
            challenge = session.scalar(select(Challenge).where(Challenge.user_id == user_id,
                                                               Challenge.consumed.is_(False)).order_by(Challenge.expires_at.desc()))
            if challenge is None or challenge.expires_at <= utc_now_naive():
                raise DomainError("OTP_EXPIRED")
            if challenge.attempts >= 3:
                raise DomainError("LOCKED")
            challenge.attempts += 1
            if not secrets.compare_digest(challenge.code_hash, _hash(code)):
                raise DomainError("OTP_INVALID")
            challenge.consumed = True
            session.flush()
            return challenge
