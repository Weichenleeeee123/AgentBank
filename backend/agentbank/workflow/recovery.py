from __future__ import annotations

from dataclasses import dataclass, field
import uuid
import json
from typing import Callable
from sqlalchemy import select

from agentbank.clock import utc_now_naive
from agentbank.db import Database, HandoffCase, Operation
from agentbank.errors import DomainError
from agentbank.audit.store import PersistentAudit
from agentbank.security.dlp import redact_text
from agentbank.db import PlanRecord, PlanNodeRecord, ReceiptRecord


@dataclass(frozen=True)
class SagaResult:
    status: str
    completed_steps: list[str]
    failed_step: str | None = None
    failed_compensations: list[str] = field(default_factory=list)


class SagaRunner:
    def run(self, steps: list[tuple[str, Callable[[], object], Callable[[], object]]]) -> SagaResult:
        completed: list[tuple[str, Callable[[], object]]] = []
        try:
            for name, action, compensation in steps:
                action()
                completed.append((name, compensation))
        except Exception:
            failures: list[str] = []
            for completed_name, compensation in reversed(completed):
                try:
                    compensation()
                except Exception:
                    failures.append(completed_name)
            return SagaResult("partially_completed", [item for item, _ in completed], name, failures)
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
        with self.database.atomic() as session:
            plan_id = reason.split(":", 1)[0] if operation_type == "plan" else ""
            plan = session.get(PlanRecord, plan_id) if plan_id else None
            if plan is not None and plan.user_id == user_id:
                nodes = session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan.id)).all()
                receipts = session.scalars(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan.id)).all()
                case.context_json = json.dumps({"plan_id": plan.id, "revision": plan.revision,
                                                "nodes": [{"id": node.id, "status": node.status} for node in nodes],
                                                "authorization": [{"id": receipt.id, "status": receipt.status} for receipt in receipts]})
            case.reason = redact_text(reason)
            session.add(case)
            PersistentAudit(self.database).append(session, f"trace_{case.id}", user_id,
                                                  "handoff.opened", {"case_id": case.id, "reason": case.reason})
        return HandoffView(case.id, case.status)

    def list_open(self) -> list[dict[str, str]]:
        with self.database.sessions() as session:
            cases = session.scalars(select(HandoffCase).where(HandoffCase.status == "open").order_by(HandoffCase.created_at)).all()
            return [{"id": case.id, "user_id": case.user_id, "operation_type": case.operation_type,
                     "reason": case.reason, "status": case.status, "created_at": case.created_at.isoformat(),
                     "context_json": case.context_json} for case in cases]

    def resolve(self, case_id: str, note: str) -> HandoffView:
        with self.database.sessions.begin() as session:
            case = session.get(HandoffCase, case_id)
            if case is None:
                raise DomainError("HANDOFF_NOT_FOUND")
            if case.status != "open":
                raise DomainError("HANDOFF_STATE_INVALID")
            case.status = "resolved"
            case.resolution_note = redact_text(note)
            case.resolved_at = utc_now_naive()
            PersistentAudit(self.database).append(session, f"trace_{case.id}", case.user_id,
                                                  "staff.handoff.resolved", {"case_id": case.id,
                                                                             "note": case.resolution_note})
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
