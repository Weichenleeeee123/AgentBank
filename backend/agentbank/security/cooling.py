from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid

from agentbank.db import CoolingPeriod, Database
from agentbank.errors import DomainError
from agentbank.clock import utc_now_naive


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CoolingView:
    id: str
    expires_at: datetime


class CoolingPeriodService:
    def __init__(self, database: Database, duration: timedelta = timedelta(minutes=30)) -> None:
        self.database, self.duration = database, duration

    def start(self, user_id: str, plan_id: str, amount_minor: int) -> CoolingView:
        started = datetime.now(timezone.utc)
        expires = started + self.duration
        cooling = CoolingPeriod(id=f"cool_{uuid.uuid4().hex}", user_id=user_id, plan_id=plan_id,
                                amount_minor=amount_minor, started_at=started.replace(tzinfo=None),
                                expires_at=expires.replace(tzinfo=None))
        with self.database.sessions.begin() as session:
            session.add(cooling)
        return CoolingView(cooling.id, expires)

    def is_blocked(self, cooling_id: str, now: datetime | None = None) -> bool:
        with self.database.sessions() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None or cooling.status != "active":
                return False
            return _aware(cooling.expires_at) > (now or datetime.now(timezone.utc))

    def release(self, cooling_id: str, *, now: datetime | None = None, reconfirmed: bool = False) -> bool:
        with self.database.sessions.begin() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None:
                raise DomainError("COOLING_NOT_FOUND")
            current = now or datetime.now(timezone.utc)
            if _aware(cooling.expires_at) > current:
                raise DomainError("COOLING_PERIOD_ACTIVE")
            if not reconfirmed:
                raise DomainError("RECONFIRMATION_REQUIRED")
            cooling.status = "released"
            cooling.reconfirmed_at = utc_now_naive()
            return True
