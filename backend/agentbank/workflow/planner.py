from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from agentbank.contracts import Plan, PlanNode
from agentbank.db import Database, Event, PlanNodeRecord, PlanRecord, ReceiptRecord
from agentbank.errors import DomainError
from agentbank.planning.validator import PlanValidator
from agentbank.bank.capabilities_full import CapabilityService


class BusinessPlanService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.registry = {
            "events.lookup": {"kind": "query"}, "funds.reserve": {"kind": "command"},
            "merchant.order.create": {"kind": "command"}, "funds.release": {"kind": "command"},
        }

    def create_birthday_plan(self, user_id: str, event_id: str, reserve_minor: int) -> Plan:
        with self.database.sessions() as session:
            event = session.get(Event, event_id)
            if event is None or event.user_id != user_id:
                raise DomainError("EVENT_NOT_FOUND")
        plan_id = f"plan_{uuid.uuid4().hex}"
        nodes = [
            PlanNode(id="event", tool="events.lookup", args={"event_id": event_id}, idempotency_key=f"{plan_id}-event"),
            PlanNode(id="reserve", tool="funds.reserve", args={"amount_minor": reserve_minor, "event_id": event_id},
                     depends_on=["event"], compensation="funds.release", idempotency_key=f"{plan_id}-reserve"),
            PlanNode(id="flowers", tool="merchant.order.create", args={"item": "flower", "event_id": event_id},
                     depends_on=["reserve"], compensation="funds.release", idempotency_key=f"{plan_id}-flowers"),
            PlanNode(id="cake", tool="merchant.order.create", args={"item": "cake", "event_id": event_id},
                     depends_on=["reserve"], compensation="funds.release", idempotency_key=f"{plan_id}-cake"),
        ]
        PlanValidator(self.registry).validate([node.model_dump() for node in nodes])
        created = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.database.sessions.begin() as session:
            display_hash = f"sha256:{uuid.uuid4().hex}"
            action_digest = f"sha256:{uuid.uuid4().hex}"
            session.add(PlanRecord(id=plan_id, user_id=user_id, revision=1, status="awaiting_confirmation",
                                   display_hash=display_hash, action_hash=action_digest, created_at=created))
            session.add(ReceiptRecord(id=f"rcp_{uuid.uuid4().hex}", subject_id=user_id, plan_id=plan_id,
                                      plan_revision=1, node_ids_json=json.dumps([node.id for node in nodes]),
                                      action_hash=action_digest, display_hash=display_hash, assurance="confirm",
                                      issued_at=created, expires_at=created + timedelta(minutes=5), status="pending"))
            for node in nodes:
                session.add(PlanNodeRecord(id=f"{plan_id}-{node.id}", plan_id=plan_id, tool=node.tool,
                                           args_json=json.dumps(node.model_dump(), ensure_ascii=False), status="pending",
                                           idempotency_key=node.idempotency_key))
        return Plan(plan_id=plan_id, revision=1, status="awaiting_confirmation", user_id=user_id,
                    nodes=nodes, display=[{"label": "资金预留", "value": str(reserve_minor)}], created_at=created)

    def graph(self, plan_id: str, user_id: str) -> dict[str, Any]:
        with self.database.sessions() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            records = session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id)).all()
            nodes: list[dict[str, Any]] = []
            for record in records:
                data = json.loads(record.args_json)
                nodes.append({"node_id": record.id, "logical_id": data["id"], "tool": record.tool,
                              "status": record.status, "depends_on": data.get("depends_on", []),
                              "authorization": "confirm" if record.tool in {"funds.reserve", "merchant.order.create"} else "none",
                              "compensation": data.get("compensation")})
            return {"plan_id": plan_id, "status": plan.status, "revision": plan.revision, "nodes": nodes}

    def revise_birthday_plan(self, user_id: str, plan_id: str, changes: dict[str, Any]) -> Plan:
        with self.database.sessions() as session:
            current = session.get(PlanRecord, plan_id)
            if current is None or current.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if current.status in {"succeeded", "cancelled"}:
                raise DomainError("PLAN_NOT_EDITABLE")
            event_node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id,
                                                                       PlanNodeRecord.tool == "events.lookup"))
            if event_node is None:
                raise DomainError("PLAN_INVALID")
            event_id = json.loads(event_node.args_json)["args"]["event_id"]
            reserve_minor = int(changes.get("reserve_minor", 100_000))
        with self.database.sessions.begin() as session:
            current = session.get(PlanRecord, plan_id)
            if current is None:
                raise DomainError("PLAN_NOT_FOUND")
            current.status = "invalidated"
            from agentbank.db import ReceiptRecord
            for receipt in session.scalars(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan_id)).all():
                receipt.status = "invalidated"
        revised = self.create_birthday_plan(user_id, event_id, reserve_minor)
        with self.database.sessions.begin() as session:
            new_record = session.get(PlanRecord, revised.plan_id)
            if new_record is not None:
                new_record.revision = current.revision + 1 if current else 2
        return Plan(plan_id=revised.plan_id, revision=2, status=revised.status, user_id=user_id,
                    nodes=revised.nodes, display=revised.display, created_at=revised.created_at)

    def execute_plan(self, user_id: str, plan_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise DomainError("CONFIRMATION_REQUIRED")
        with self.database.sessions() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            records = list(session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id).order_by(PlanNodeRecord.id)).all())
        capabilities = CapabilityService(self.database)
        reservation_id: str | None = None
        results: list[dict[str, Any]] = []
        for record in records:
            data = json.loads(record.args_json)
            if data["id"] == "event":
                status = "succeeded"
                result_data = {"event_id": data["args"]["event_id"]}
            elif data["id"] == "reserve":
                result = capabilities.execute(user_id, "X02", {"amount_minor": data["args"]["amount_minor"], "plan_id": plan_id}, f"{plan_id}-reserve")
                reservation_id = str(result.data["reservation_id"])
                status, result_data = result.status, result.data
            else:
                capability_id = "X03" if data["args"]["item"] == "flower" else "X04"
                result = capabilities.execute(user_id, capability_id, {"delivery_date": "2026-11-13", "reservation_id": reservation_id}, f"{plan_id}-{data['id']}")
                status, result_data = result.status, result.data
            with self.database.sessions.begin() as session:
                current = session.get(PlanNodeRecord, record.id)
                if current is not None:
                    current.status = status
            results.append({"node_id": data["id"], "status": status, "data": result_data})
        final_status = "succeeded" if all(item["status"] in {"succeeded", "scheduled", "reserved"} for item in results) else "partially_completed"
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is not None:
                plan.status = final_status
        return {"plan_id": plan_id, "status": final_status, "nodes": results}
