from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from agentbank.db import Database, Transaction

ALLOWED_FIELDS = {"category", "merchant", "posted_at", "amount_minor", "direction"}
ALLOWED_OPS = {"eq", "between", "gte", "lte"}
ALLOWED_GROUPS = {"month", "category", "merchant", "direction"}


@dataclass(frozen=True)
class DslResult:
    rows: list[dict[str, Any]]
    query_hash: str
    row_count: int


class DslExecutor:
    def __init__(self, database: Database) -> None:
        self.database = database

    def run(self, user_id: str, dsl: dict[str, Any]) -> DslResult:
        canonical = json.dumps(dsl, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        query_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if dsl.get("source") != "transactions":
            raise ValueError("DSL_SOURCE_NOT_ALLOWED")
        limit = int(dsl.get("limit", 100))
        if limit < 1 or limit > 50_000:
            raise ValueError("DSL_LIMIT_EXCEEDED")
        filters = dsl.get("filters", [])
        for item in filters:
            if item.get("field") not in ALLOWED_FIELDS or item.get("op") not in ALLOWED_OPS:
                raise ValueError("DSL_FIELD_NOT_ALLOWED")
        groups = list(dsl.get("group_by", []))
        if any(group not in ALLOWED_GROUPS for group in groups):
            raise ValueError("DSL_GROUP_NOT_ALLOWED")
        with self.database.sessions() as session:
            records = list(session.scalars(select(Transaction).where(Transaction.user_id == user_id)).all())
        records = [record for record in records if self._matches(record, filters)]
        if groups:
            rows = self._group(records, groups)
        else:
            rows = [self._record_row(record) for record in records]
        rows = rows[:limit]
        return DslResult(rows=rows, query_hash=query_hash, row_count=len(rows))

    def _matches(self, record: Transaction, filters: list[dict[str, Any]]) -> bool:
        for item in filters:
            value = getattr(record, str(item["field"]))
            op = item["op"]
            expected: Any = item.get("value")
            if op == "eq" and value != expected:
                return False
            if op == "gte" and value < expected:
                return False
            if op == "lte" and value > expected:
                return False
            if op == "between" and (not isinstance(expected, list) or len(expected) != 2 or not (expected[0] <= value <= expected[1])):
                return False
        return True

    @staticmethod
    def _record_row(record: Transaction) -> dict[str, Any]:
        return {"user_id": record.user_id, "txn_ids": [record.id], "txn_id": record.id,
                "posted_at": record.posted_at, "amount_minor": record.amount_minor,
                "merchant": record.merchant, "category": record.category,
                "direction": record.direction}

    def _group(self, records: list[Transaction], groups: list[str]) -> list[dict[str, Any]]:
        grouped: dict[tuple[Any, ...], list[Transaction]] = defaultdict(list)
        for record in records:
            key = tuple(self._group_value(record, group) for group in groups)
            grouped[key].append(record)
        rows: list[dict[str, Any]] = []
        for key, items in sorted(grouped.items(), key=lambda pair: pair[0]):
            row: dict[str, Any] = {group: value for group, value in zip(groups, key, strict=True)}
            row.update({"user_id": items[0].user_id, "sum_amount_minor": sum(item.amount_minor for item in items),
                        "count": len(items), "txn_ids": [item.id for item in items]})
            rows.append(row)
        return rows

    @staticmethod
    def _group_value(record: Transaction, group: str) -> str:
        if group == "month":
            return record.posted_at[:7]
        return str(getattr(record, group))
