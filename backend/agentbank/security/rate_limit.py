from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from agentbank.errors import DomainError


class SlidingWindowLimiter:
    def __init__(self, limit: int = 30, window: timedelta = timedelta(minutes=10)) -> None:
        self.limit = limit
        self.window = window
        self._events: dict[str, deque[datetime]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = datetime.now(timezone.utc)
        events = self._events[key]
        cutoff = now - self.window
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= self.limit:
            raise DomainError("RATE_LIMITED")
        events.append(now)
