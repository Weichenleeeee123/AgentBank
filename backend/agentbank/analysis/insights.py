from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import select

from agentbank.db import Database, Transaction


def _transactions(database: Database, user_id: str) -> list[Transaction]:
    with database.sessions() as session:
        return list(session.scalars(select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.posted_at)).all())


def monthly_report(database: Database, user_id: str, start_month: str, end_month: str) -> list[dict[str, Any]]:
    start = date.fromisoformat(start_month + "-01")
    end = date.fromisoformat(end_month + "-01")
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"income_minor": 0, "expense_minor": 0, "transfer_minor": 0})
    for transaction in _transactions(database, user_id):
        month = transaction.posted_at[:7]
        if start_month <= month <= end_month:
            key = "income_minor" if transaction.direction == "credit" else "expense_minor"
            if transaction.direction == "transfer":
                key = "transfer_minor"
            buckets[month][key] += transaction.amount_minor
    rows: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        month = cursor.strftime("%Y-%m")
        values = buckets.get(month, {"income_minor": 0, "expense_minor": 0, "transfer_minor": 0})
        rows.append({"month": month, **values, "missing": month not in buckets})
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    return rows


def detect_subscriptions(database: Database, user_id: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for transaction in _transactions(database, user_id):
        if transaction.direction == "debit":
            groups[transaction.merchant].append(transaction)
    output: list[dict[str, Any]] = []
    for merchant, records in groups.items():
        records.sort(key=lambda item: item.posted_at)
        dates = [datetime.fromisoformat(item.posted_at).date() for item in records]
        intervals = [(right - left).days for left, right in zip(dates, dates[1:], strict=False)]
        amounts = [item.amount_minor for item in records]
        regular = len(records) >= 3 and bool(intervals)
        if regular:
            interval_median = median(intervals)
            mean = sum(intervals) / len(intervals)
            variance = sum((value - mean) ** 2 for value in intervals) / len(intervals)
            coefficient = (variance ** 0.5) / mean if mean else 99
            regular = (6 <= interval_median <= 8 or 26 <= interval_median <= 35 or 360 <= interval_median <= 370) and coefficient <= 0.15
            regular = regular and (max(amounts) - min(amounts)) / median(amounts) <= 0.15
        output.append({"merchant": merchant, "status": "confirmed" if regular else "suspected",
                       "evidence_txn_ids": [item.id for item in records],
                       "price_increase": len(amounts) >= 2 and amounts[-1] > median(amounts[:-1]) * 1.10})
    return output


def detect_anomalies(database: Database, user_id: str) -> list[dict[str, Any]]:
    records = _transactions(database, user_id)
    output: list[dict[str, Any]] = []
    for left_index, left in enumerate(records):
        for right in records[left_index + 1:]:
            if left.merchant == right.merchant and left.amount_minor == right.amount_minor:
                try:
                    delta = abs(datetime.fromisoformat(right.posted_at) - datetime.fromisoformat(left.posted_at))
                except ValueError:
                    continue
                if delta <= timedelta(minutes=5):
                    output.append({"reason_code": "DUP_CHARGE", "txn_ids": [left.id, right.id],
                                   "evidence": f"same merchant and amount within {delta.total_seconds():.0f}s"})
    return output
