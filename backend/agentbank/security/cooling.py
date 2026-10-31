from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid

from agentbank.db import CoolingPeriod, Database
from agentbank.errors import DomainError


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CoolingView:
    id: str
    expires_at: datetime
    reconfirm_deadline: datetime
    action_hash: str = ""
    plan_revision: int = 1


class CoolingPeriodService:
    def __init__(self, database: Database, duration: timedelta = timedelta(minutes=30),
                 reconfirm_window: timedelta = timedelta(minutes=10)) -> None:
        self.database, self.duration, self.reconfirm_window = database, duration, reconfirm_window

    def start(self, user_id: str, plan_id: str, amount_minor: int, *, action_hash: str = "",
              plan_revision: int = 1, now: datetime | None = None) -> CoolingView:
        started = _aware(now or datetime.now(timezone.utc))
        expires = started + self.duration
        reconfirm_deadline = expires + self.reconfirm_window
        with self.database.sessions.begin() as session:
            cooling = self._build(user_id, plan_id, amount_minor, action_hash, plan_revision,
                                  started, expires, reconfirm_deadline)
            session.add(cooling)
        return CoolingView(cooling.id, expires, reconfirm_deadline, action_hash, plan_revision)

    def start_in_session(self, session: object, user_id: str, plan_id: str, amount_minor: int, *,
                         action_hash: str = "", plan_revision: int = 1,
                         now: datetime | None = None) -> CoolingView:
        started = _aware(now or datetime.now(timezone.utc))
        expires = started + self.duration
        reconfirm_deadline = expires + self.reconfirm_window
        cooling = self._build(user_id, plan_id, amount_minor, action_hash, plan_revision,
                              started, expires, reconfirm_deadline)
        session.add(cooling)  # type: ignore[attr-defined]
        return CoolingView(cooling.id, expires, reconfirm_deadline, action_hash, plan_revision)

    @staticmethod
    def _build(user_id: str, plan_id: str, amount_minor: int, action_hash: str,
               plan_revision: int, started: datetime, expires: datetime,
               reconfirm_deadline: datetime) -> CoolingPeriod:
        return CoolingPeriod(id=f"cool_{uuid.uuid4().hex}", user_id=user_id, plan_id=plan_id,
                             plan_revision=plan_revision, action_hash=action_hash,
                             amount_minor=amount_minor, started_at=started.replace(tzinfo=None),
                             expires_at=expires.replace(tzinfo=None),
                             reconfirm_deadline=reconfirm_deadline.replace(tzinfo=None))

    def is_blocked(self, cooling_id: str, now: datetime | None = None) -> bool:
        with self.database.sessions() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None or cooling.status != "active":
                return False
            return _aware(cooling.expires_at) > (now or datetime.now(timezone.utc))

    def release(self, cooling_id: str, *, action_hash: str = "", plan_revision: int | None = None,
                now: datetime | None = None, reconfirmed: bool = False) -> bool:
        with self.database.sessions.begin() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None:
                raise DomainError("COOLING_NOT_FOUND")
            if cooling.status != "active":
                if cooling.status == "completed":
                    return True
                raise DomainError("COOLING_NOT_ACTIVE")
            if action_hash and cooling.action_hash and action_hash != cooling.action_hash:
                raise DomainError("ACTION_CHANGED")
            if plan_revision is not None and plan_revision != cooling.plan_revision:
                raise DomainError("ACTION_CHANGED")
            current = _aware(now or datetime.now(timezone.utc))
            if _aware(cooling.expires_at) > current:
                raise DomainError("COOLING_PERIOD_ACTIVE")
            if not reconfirmed:
                raise DomainError("COOLING_RECONFIRMATION_REQUIRED")
            deadline = _aware(cooling.reconfirm_deadline) if cooling.reconfirm_deadline else _aware(cooling.expires_at) + self.reconfirm_window
            if current > deadline:
                raise DomainError("COOLING_RECONFIRMATION_REQUIRED")
            cooling.status = "completed"
            cooling.reconfirmed_at = current.replace(tzinfo=None)
            return True

    def invalidate(self, cooling_id: str) -> bool:
        with self.database.sessions.begin() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None:
                raise DomainError("COOLING_NOT_FOUND")
            if cooling.status == "active":
                cooling.status = "invalidated"
            return cooling.status == "invalidated"

    def cancel(self, cooling_id: str) -> bool:
        with self.database.sessions.begin() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None:
                raise DomainError("COOLING_NOT_FOUND")
            if cooling.status == "active":
                cooling.status = "cancelled"
            return cooling.status == "cancelled"
