"""Staff-only local operational observations and safe handoff intent editing."""
from __future__ import annotations

from collections import Counter, deque
from datetime import timedelta
import hmac
import os
from typing import Annotated, Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from agentbank.clock import utc_now_naive
from agentbank.db import AuditLog, Database, LLMUsage, SecurityCounter, SecurityLock
from agentbank.errors import DomainError
from agentbank.workflow.recovery import HandoffService


class StaffNote(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    note: str = Field(min_length=1, max_length=2000)


class StaffDraft(StaffNote):
    user_message: str = Field(default="", max_length=2000)


def require_staff(x_staff_token: Annotated[str | None, Header()] = None) -> None:
    expected = os.getenv("AGENTBANK_STAFF_TOKEN", "")
    if not expected or not x_staff_token or not hmac.compare_digest(
        expected.encode(), x_staff_token.encode()
    ):
        raise HTTPException(status_code=403, detail={"code": "STAFF_REQUIRED"})


def register_staff_routes(app: FastAPI, database: Database) -> None:
    # Parent middleware appends (UTC naive datetime, HTTP status, path); no user data.
    app.state.request_metrics = deque(maxlen=10000)
    app.state.request_metrics_started_at = utc_now_naive()

    def domain_response(action: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            return action()
        except DomainError as exc:
            status = 404 if exc.code == "HANDOFF_NOT_FOUND" else (
                422 if exc.code == "HANDOFF_INPUT_INVALID" else 409)
            raise HTTPException(status_code=status, detail={"code": exc.code}) from exc

    @app.post("/staff/v1/handoffs/{case_id}/draft", dependencies=[Depends(require_staff)])
    def draft(case_id: str, payload: StaffDraft) -> dict[str, object]:
        return domain_response(lambda: HandoffService(database).save_draft(
            case_id, note=payload.note, user_message=payload.user_message))

    @app.post("/staff/v1/handoffs/{case_id}/retry-request", dependencies=[Depends(require_staff)])
    def retry(case_id: str, payload: StaffNote) -> dict[str, object]:
        return domain_response(lambda: HandoffService(database).request_retry(case_id, payload.note))

    @app.get("/staff/v1/metrics", dependencies=[Depends(require_staff)])
    def metrics() -> dict[str, object]:
        now = utc_now_naive()
        since = now - timedelta(hours=1)
        observations = [row for row in list(app.state.request_metrics) if row[0] >= since]
        with database.sessions() as session:
            events = session.scalars(select(AuditLog.event_type).where(
                AuditLog.occurred_at >= since,
                AuditLog.event_type.in_(["llm.call.completed", "llm.call.failed"]))).all()
            failures = events.count("llm.call.failed")
            locks = session.scalars(select(SecurityLock).where(SecurityLock.locked_until > now)).all()
            counter_count = len(session.scalars(select(SecurityCounter.id).where(
                SecurityCounter.occurred_at >= since)).all())
            recent_users = Counter(session.scalars(select(SecurityCounter.user_id).where(
                SecurityCounter.occurred_at >= now - timedelta(minutes=30))).all())
            active_users = {lock.user_id for lock in locks} | {
                user_id for user_id, count in recent_users.items() if count >= 3}
            usage = session.get(LLMUsage, now.strftime("%Y-%m-%dT%H"))
            calls = usage.calls if usage else 0
        limit = max(0, int(os.getenv("AGENTBANK_LLM_MAX_CALLS_PER_HOUR", "1000")))
        snapshot = getattr(app.state, "tool_breaker_snapshot", None)
        tools = snapshot() if callable(snapshot) else {"status": "not_measured"}
        return {
            "scope": "local candidate; retained process requests and database observations",
            "window_start": since.isoformat(), "observed_at": now.isoformat(),
            "requests": {"count": len(observations),
                         "server_errors": sum(row[1] >= 500 for row in observations),
                         "retention_limit": 10000,
                         "process_started_at": app.state.request_metrics_started_at.isoformat()},
            "model": {"completed": len(events) - failures, "failed": failures,
                      "failure_rate": failures / len(events) if events else None,
                      "source": "llm.call.completed/failed audit events in rolling hour"},
            "user_breakers": {"active": len(active_users), "security_events_last_hour": counter_count},
            "tool_breakers": tools,
            "llm_budget": {"window": now.strftime("%Y-%m-%dT%H"), "calls": calls,
                           "limit": limit, "remaining": max(0, limit - calls)},
        }
