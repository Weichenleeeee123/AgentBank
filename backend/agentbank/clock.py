"""Business clock abstraction; security-sensitive expiry uses real UTC in M1."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def business_date() -> str:
    from zoneinfo import ZoneInfo

    return utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
