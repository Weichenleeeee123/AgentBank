from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterator, cast

from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from agentbank.audit.store import PersistentAudit
from agentbank.db import Database, Transaction

ALLOWED_FIELDS = {"category", "merchant", "posted_at", "amount_minor", "direction"}
ALLOWED_OPS = {"eq", "between", "gte", "lte"}
ALLOWED_GROUPS = {"month", "category", "merchant", "direction"}
ALLOWED_METRICS = {"sum", "count"}
DSL_FIELDS = {"source", "filters", "group_by", "metrics", "order_by", "limit"}


@dataclass(frozen=True)
class DslResult:
    rows: list[dict[str, Any]]
    query_hash: str
    row_count: int


class DslExecutor:
    def __init__(self, database: Database) -> None:
        self.database = database

    def run(self, user_id: str, dsl: dict[str, Any]) -> DslResult:
        if not isinstance(dsl, dict) or set(dsl) - DSL_FIELDS:
            raise ValueError("DSL_FIELD_NOT_ALLOWED")
        canonical = json.dumps(dsl, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        query_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if dsl.get("source") != "transactions":
            raise ValueError("DSL_SOURCE_NOT_ALLOWED")
        raw_limit = dsl.get("limit", 100)
        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
            raise ValueError("DSL_LIMIT_EXCEEDED")
        limit = raw_limit
        if limit < 1 or limit > 50_000:
            raise ValueError("DSL_LIMIT_EXCEEDED")
        filters = dsl.get("filters", [])
        if not isinstance(filters, list):
            raise ValueError("DSL_FILTER_INVALID")
        for item in filters:
            if not isinstance(item, dict) or set(item) != {"field", "op", "value"}:
                raise ValueError("DSL_FILTER_INVALID")
            field = item.get("field")
            op = item.get("op")
            expected = item.get("value")
            if not isinstance(field, str) or not isinstance(op, str) or field not in ALLOWED_FIELDS or op not in ALLOWED_OPS:
                raise ValueError("DSL_FIELD_NOT_ALLOWED")
            if op == "between" and (not isinstance(expected, list) or len(expected) != 2):
                raise ValueError("DSL_FILTER_VALUE_INVALID")
            values = cast(list[Any], expected) if op == "between" else [expected]
            expected_type = int if field == "amount_minor" else str
            if any(not isinstance(value, expected_type) or isinstance(value, bool) for value in values):
                raise ValueError("DSL_FILTER_VALUE_INVALID")
        raw_groups = dsl.get("group_by", [])
        if not isinstance(raw_groups, list) or any(not isinstance(group, str) for group in raw_groups):
            raise ValueError("DSL_GROUP_NOT_ALLOWED")
        groups = list(raw_groups)
        if len(groups) != len(set(groups)) or any(group not in ALLOWED_GROUPS for group in groups):
            raise ValueError("DSL_GROUP_NOT_ALLOWED")
        metrics = dsl.get("metrics", [])
        if not isinstance(metrics, list):
            raise ValueError("DSL_METRIC_NOT_ALLOWED")
        for metric in metrics:
            if not isinstance(metric, dict) or set(metric) - {"fn", "field"}:
                raise ValueError("DSL_METRIC_NOT_ALLOWED")
            if not isinstance(metric.get("fn"), str) or metric.get("fn") not in ALLOWED_METRICS:
                raise ValueError("DSL_METRIC_NOT_ALLOWED")
            if metric.get("fn") == "sum" and metric.get("field") != "amount_minor":
                raise ValueError("DSL_FIELD_NOT_ALLOWED")
            if metric.get("fn") == "count" and "field" in metric:
                raise ValueError("DSL_METRIC_NOT_ALLOWED")
        if len({metric["fn"] for metric in metrics}) != len(metrics):
            raise ValueError("DSL_METRIC_NOT_ALLOWED")
        orderings = dsl.get("order_by", [])
        if not isinstance(orderings, list):
            raise ValueError("DSL_ORDER_NOT_ALLOWED")
        output_fields = set(groups)
        if not groups and not metrics:
            output_fields |= ALLOWED_FIELDS
        selected_metrics = {metric["fn"] for metric in metrics} or ({"sum", "count"} if groups else set())
        if "sum" in selected_metrics:
            output_fields.add("sum_amount_minor")
        if "count" in selected_metrics:
            output_fields.add("count")
        for ordering in orderings:
            if not isinstance(ordering, dict) or set(ordering) - {"field", "dir"}:
                raise ValueError("DSL_ORDER_NOT_ALLOWED")
            if (not isinstance(ordering.get("field"), str) or ordering.get("field") not in output_fields
                    or ordering.get("dir", "asc") not in {"asc", "desc"}):
                raise ValueError("DSL_ORDER_NOT_ALLOWED")
        started = perf_counter()
        with self._readonly_session() as session:
            statement = select(Transaction).where(Transaction.user_id == user_id)
            for item in filters:
                column = getattr(Transaction, str(item["field"]))
                expected = item.get("value")
                op = item["op"]
                if op == "eq":
                    statement = statement.where(column == expected)
                elif op == "gte":
                    statement = statement.where(column >= expected)
                elif op == "lte":
                    statement = statement.where(column <= expected)
                elif op == "between":
                    statement = statement.where(column.between(expected[0], expected[1]))
            records = list(session.scalars(statement.order_by(Transaction.posted_at).limit(50_001)).all())
            if perf_counter() - started > 2.0:
                raise ValueError("DSL_TIMEOUT")
        if len(records) > 50_000:
            raise ValueError("DSL_ROW_LIMIT_EXCEEDED")
        if groups or metrics:
            rows = self._group(records, groups, metrics)
        else:
            rows = [self._record_row(record) for record in records]
        for ordering in reversed(orderings):
            key = str(ordering["field"])
            rows.sort(key=lambda row: cast(Any, row[key]), reverse=ordering.get("dir", "asc") == "desc")
        rows = rows[:limit]
        with self.database.sessions.begin() as session:
            PersistentAudit(self.database).append(session, f"trace_dsl_{query_hash[7:19]}", user_id,
                                                   "analysis.dsl.executed",
                                                   {"query_hash": query_hash, "row_count": len(rows)})
        return DslResult(rows=rows, query_hash=query_hash, row_count=len(rows))

    @contextmanager
    def _readonly_session(self) -> Iterator[Session]:
        source_url = make_url(str(self.database.engine.url))
        database_path = source_url.database
        if source_url.get_backend_name() == "sqlite" and database_path not in {None, "", ":memory:"}:
            resolved = Path(str(database_path)).resolve().as_posix()
            readonly_url = URL.create("sqlite+pysqlite", database=f"file:{resolved}",
                                      query={"mode": "ro", "uri": "true"})
            from sqlalchemy import create_engine

            engine = create_engine(readonly_url, connect_args={"uri": True})
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            try:
                with factory() as session:
                    connection = session.connection()
                    raw_connection = cast(Any, connection.connection.driver_connection)
                    started = perf_counter()
                    raw_connection.set_progress_handler(lambda: 1 if perf_counter() - started > 2.0 else 0, 1_000)
                    yield session
                    raw_connection.set_progress_handler(None, 0)
            finally:
                engine.dispose()
            return
        with self.database.sessions() as session:
            connection = session.connection()
            connection.exec_driver_sql("PRAGMA query_only=ON")
            raw_connection = cast(Any, connection.connection.driver_connection)
            started = perf_counter()
            raw_connection.set_progress_handler(lambda: 1 if perf_counter() - started > 2.0 else 0, 1_000)
            try:
                yield session
            finally:
                raw_connection.set_progress_handler(None, 0)
                raw_connection.execute("PRAGMA query_only=OFF")

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

    def _group(self, records: list[Transaction], groups: list[str],
               metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[Any, ...], list[Transaction]] = defaultdict(list)
        for record in records:
            key = tuple(self._group_value(record, group) for group in groups)
            grouped[key].append(record)
        rows: list[dict[str, Any]] = []
        for key, items in sorted(grouped.items(), key=lambda pair: pair[0]):
            row: dict[str, Any] = {group: value for group, value in zip(groups, key, strict=True)}
            if "sum" in {metric["fn"] for metric in metrics} or not metrics:
                row["sum_amount_minor"] = sum(item.amount_minor for item in items)
            if "count" in {metric["fn"] for metric in metrics} or not metrics:
                row["count"] = len(items)
            row.update({"user_id": items[0].user_id, "txn_ids": [item.id for item in items]})
            rows.append(row)
        return rows

    @staticmethod
    def _group_value(record: Transaction, group: str) -> str:
        if group == "month":
            return record.posted_at[:7]
        return str(getattr(record, group))
