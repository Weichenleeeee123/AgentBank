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
    for index, row in enumerate(rows):
        previous = rows[index - 1] if index else None
        previous_year = buckets.get(f"{int(row['month'][:4]) - 1}-{row['month'][5:]}")
        current_total = row["expense_minor"]
        previous_total = previous["expense_minor"] if previous else None
        yoy_total = previous_year["expense_minor"] if previous_year else None
        row["mom_change"] = None if previous_total in (None, 0) else round((current_total - previous_total) / previous_total, 4)
        row["yoy_change"] = None if yoy_total in (None, 0) else round((current_total - yoy_total) / yoy_total, 4)
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


def birthday_history_clues(database: Database, user_id: str, event_date: str) -> list[dict[str, Any]]:
    target = date.fromisoformat(event_date)
    records = _transactions(database, user_id)
    groups: dict[str, list[str]] = defaultdict(list)
    for record in records:
        if record.direction != "debit":
            continue
        try:
            posted = date.fromisoformat(record.posted_at[:10]).replace(year=target.year)
        except ValueError:
            continue
        distance = abs((posted - target).days)
        if distance > 7 and distance < 358:
            continue
        merchant = record.merchant.lower()
        category = "flower" if "花" in merchant or "flower" in merchant else "cake" if "蛋糕" in merchant or "cake" in merchant else ""
        if category:
            groups[category].append(record.id)
    return [{"category": category, "txn_ids": txn_ids, "source": "historical_transactions"}
            for category, txn_ids in sorted(groups.items())]


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
    by_category: dict[str, list[Transaction]] = defaultdict(list)
    for record in records:
        if record.direction == "debit":
            by_category[record.category].append(record)
    for category, items in by_category.items():
        if len(items) < 4:
            continue
        amounts = sorted(item.amount_minor for item in items)
        midpoint = len(amounts) // 2
        med = amounts[midpoint] if len(amounts) % 2 else (amounts[midpoint - 1] + amounts[midpoint]) / 2
        deviations = sorted(abs(amount - med) for amount in amounts)
        mad_mid = len(deviations) // 2
        mad = deviations[mad_mid] if len(deviations) % 2 else (deviations[mad_mid - 1] + deviations[mad_mid]) / 2
        if mad <= 0:
            continue
        for record in items:
            robust_z = 0.6745 * abs(record.amount_minor - med) / mad
            if robust_z > 3.5 and record.amount_minor > 20_000:
                output.append({"reason_code": "AMOUNT_OUTLIER", "txn_ids": [record.id],
                               "evidence": {"category": category, "median_minor": med, "mad_minor": mad,
                                            "robust_z": round(robust_z, 3)}})
    for merchant, items in _group_by_merchant(records).items():
        items.sort(key=lambda item: item.posted_at)
        for index, small in enumerate(items):
            try:
                small_time = datetime.fromisoformat(small.posted_at)
            except ValueError:
                continue
            if not (small.merchant.startswith("境外") and small.amount_minor < 500):
                continue
            for large in items[index + 1:]:
                try:
                    delta = datetime.fromisoformat(large.posted_at) - small_time
                except ValueError:
                    continue
                if timedelta(0) <= delta <= timedelta(hours=24) and large.amount_minor > 20_000:
                    output.append({"reason_code": "PROBE_THEN_LARGE", "txn_ids": [small.id, large.id],
                                   "evidence": "foreign small probe followed by large payment within 24h"})
                    break
    debit_records = [record for record in records if record.direction == "debit"]
    amounts = sorted(record.amount_minor for record in debit_records)
    if amounts:
        p95 = amounts[min(len(amounts) - 1, int(len(amounts) * 0.95))]
        seen: set[str] = set()
        for record in sorted(debit_records, key=lambda item: item.posted_at):
            if record.merchant in seen:
                continue
            seen.add(record.merchant)
            if record.amount_minor > p95:
                output.append({"reason_code": "NEW_MERCHANT_LARGE", "txn_ids": [record.id],
                               "evidence": {"debit_p95_minor": p95, "amount_minor": record.amount_minor}})
    return output


def _group_by_merchant(records: list[Transaction]) -> dict[str, list[Transaction]]:
    grouped: dict[str, list[Transaction]] = defaultdict(list)
    for record in records:
        grouped[record.merchant].append(record)
    return grouped
