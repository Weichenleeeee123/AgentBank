from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Callable
from sqlalchemy import select

from agentbank.clock import utc_now_naive
from agentbank.db import Database, HandoffCase, Operation
from agentbank.errors import DomainError


@dataclass(frozen=True)
class SagaResult:
    status: str
    completed_steps: list[str]
    failed_step: str | None = None


class SagaRunner:
    def run(self, steps: list[tuple[str, Callable[[], object], Callable[[], object]]]) -> SagaResult:
        completed: list[tuple[str, Callable[[], object]]] = []
        try:
            for name, action, compensation in steps:
                action()
                completed.append((name, compensation))
        except Exception:
            for _name, compensation in reversed(completed):
                compensation()
            return SagaResult("partially_completed", [name for name, _ in completed], name)
        return SagaResult("succeeded", [name for name, _ in completed])


@dataclass(frozen=True)
class HandoffView:
    id: str
    status: str


class HandoffService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def open(self, user_id: str, operation_type: str, reason: str) -> HandoffView:
        case = HandoffCase(id=f"handoff_{uuid.uuid4().hex}", user_id=user_id, operation_type=operation_type,
                           reason=reason, status="open", created_at=utc_now_naive())
        with self.database.sessions.begin() as session:
            session.add(case)
        return HandoffView(case.id, case.status)

    def list_open(self) -> list[dict[str, str]]:
        with self.database.sessions() as session:
            cases = session.scalars(select(HandoffCase).where(HandoffCase.status == "open").order_by(HandoffCase.created_at)).all()
            return [{"id": case.id, "user_id": case.user_id, "operation_type": case.operation_type,
                     "reason": case.reason, "status": case.status, "created_at": case.created_at.isoformat()} for case in cases]

    def resolve(self, case_id: str, note: str) -> HandoffView:
        with self.database.sessions.begin() as session:
            case = session.get(HandoffCase, case_id)
            if case is None:
                raise DomainError("HANDOFF_NOT_FOUND")
            case.status = "resolved"
            case.resolved_at = utc_now_naive()
            return HandoffView(case.id, case.status)


@dataclass(frozen=True)
class OperationRecoveryView:
    operation_id: str
    status: str
    idempotency_key: str


class UnknownRecoveryService:
    """Moves an unknown bank result to a safe queryable state without retrying side effects."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def recover(self, operation_id: str) -> OperationRecoveryView:
        with self.database.sessions.begin() as session:
            operation = session.get(Operation, operation_id)
            if operation is None:
                raise DomainError("OPERATION_NOT_FOUND")
            if operation.status == "unknown":
                operation.status = "processing"
            return OperationRecoveryView(operation.id, operation.status, operation.idempotency_key)
