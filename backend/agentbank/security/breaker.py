from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select

from agentbank.clock import utc_now_naive
from agentbank.db import Database, SecurityCounter


class CircuitBreaker:
    def __init__(self, database: Database, window: timedelta = timedelta(minutes=30), threshold: int = 3) -> None:
        self.database, self.window, self.threshold = database, window, threshold

    def record_block(self, user_id: str, reason: str) -> None:
        with self.database.sessions.begin() as session:
            session.add(SecurityCounter(id=f"sec_{uuid.uuid4().hex}", user_id=user_id, reason=reason,
                                        occurred_at=utc_now_naive()))

    def is_locked(self, user_id: str) -> bool:
        cutoff = datetime.now(timezone.utc) - self.window
        with self.database.sessions() as session:
            rows = session.scalars(select(SecurityCounter).where(SecurityCounter.user_id == user_id)).all()
            recent = [row for row in rows if row.occurred_at.replace(tzinfo=timezone.utc) >= cutoff]
            return len(recent) >= self.threshold

    def can_write(self, user_id: str, *, protective: bool) -> bool:
        return protective or not self.is_locked(user_id)
