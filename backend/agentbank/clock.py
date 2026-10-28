"""Business clock abstraction; security-sensitive expiry uses real UTC in M1."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from calendar import monthrange
from typing import Callable


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_naive() -> datetime:
    """UTC for SQLite DateTime columns, without deprecated datetime.utcnow()."""
    return utc_now().replace(tzinfo=None)


def business_date() -> str:
    from zoneinfo import ZoneInfo

    return utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


class BusinessClock:
    """Clock whose business time can be advanced without changing security time."""

    def __init__(self, initial: datetime | None = None, *, security_now_fn: Callable[[], datetime] | None = None) -> None:
        self._business_now = initial or utc_now()
        self._security_now_fn = security_now_fn or utc_now

    def now(self) -> datetime:
        return self._business_now

    def security_now(self) -> datetime:
        return self._security_now_fn()

    def business_date(self) -> str:
        from zoneinfo import ZoneInfo

        return self._business_now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()

    def advance(self, *, days: int = 0, hours: int = 0) -> datetime:
        self._business_now += timedelta(days=days, hours=hours)
        return self._business_now

    def advance_days(self, days: int) -> datetime:
        return self.advance(days=days)


def month_end(value: date) -> date:
    return value.replace(day=monthrange(value.year, value.month)[1])
