from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from agentbank.contracts import Plan, PlanNode
from agentbank.db import Database, Event, PlanNodeRecord, PlanRecord
from agentbank.errors import DomainError
from agentbank.planning.validator import PlanValidator


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
            session.add(PlanRecord(id=plan_id, user_id=user_id, revision=1, status="awaiting_confirmation",
                                   display_hash=f"sha256:{uuid.uuid4().hex}", action_hash=f"sha256:{uuid.uuid4().hex}", created_at=created))
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
