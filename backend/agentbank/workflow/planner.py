from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from agentbank.contracts import Plan, PlanNode
from agentbank.db import Database, Event, PlanNodeRecord, PlanRecord, ReceiptConsumption, ReceiptRecord
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
        display = [{"label": "活动", "value": event_id}, {"label": "资金预留（分）", "value": str(reserve_minor)},
                   {"label": "计划订单", "value": "鲜花、蛋糕"}]
        display_json = json.dumps(display, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        action_json = json.dumps([node.model_dump(mode="json") for node in sorted(nodes, key=lambda item: item.id)], ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"))
        display_hash = "sha256:" + hashlib.sha256(display_json.encode("utf-8")).hexdigest()
        action_hash = "sha256:" + hashlib.sha256(action_json.encode("utf-8")).hexdigest()
        with self.database.sessions.begin() as session:
            session.add(PlanRecord(id=plan_id, user_id=user_id, revision=1, status="awaiting_confirmation",
                                   display_hash=display_hash, action_hash=action_hash, display_json=display_json,
                                   created_at=created))
            session.add(ReceiptRecord(id=f"rcp_{uuid.uuid4().hex}", subject_id=user_id, plan_id=plan_id,
                                      plan_revision=1, node_ids_json=json.dumps([node.id for node in nodes]),
                                      action_hash=action_hash, display_hash=display_hash, assurance="confirm",
                                      issued_at=created, expires_at=created + timedelta(minutes=5), status="pending"))
            for node in nodes:
                session.add(PlanNodeRecord(id=f"{plan_id}-{node.id}", plan_id=plan_id, tool=node.tool,
                                           args_json=json.dumps(node.model_dump(), ensure_ascii=False), status="pending",
                                           idempotency_key=node.idempotency_key))
        return Plan(plan_id=plan_id, revision=1, status="awaiting_confirmation", user_id=user_id,
                    nodes=nodes, display=display, created_at=created)

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
                              "compensation": data.get("compensation"),
                              "rule_id": "P13" if record.tool == "funds.reserve" else "P14" if record.tool == "merchant.order.create" else "P03",
                              "decision_chain": [{"stage": "ownership", "status": "passed"},
                                                 {"stage": "policy", "rule_id": "P13" if record.tool == "funds.reserve" else "P14" if record.tool == "merchant.order.create" else "P03", "status": "passed"},
                                                 {"stage": "authorization", "status": "pending" if record.status == "pending" else "consumed"}]})
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

    def execute_plan(self, user_id: str, plan_id: str, receipt_id: str, display_hash: str) -> dict[str, Any]:
        with self.database.sessions() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            receipt = session.get(ReceiptRecord, receipt_id)
            if receipt is None or receipt.plan_id != plan_id or receipt.subject_id != user_id:
                raise DomainError("NOT_OWNER")
            if receipt.status in {"consumed", "invalidated"}:
                raise DomainError("RECEIPT_REPLAYED")
            if receipt.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
                raise DomainError("RECEIPT_EXPIRED")
            if receipt.plan_revision != plan.revision:
                raise DomainError("PLAN_REVISION_MISMATCH")
            if receipt.display_hash != display_hash or plan.display_hash != display_hash:
                raise DomainError("HASH_MISMATCH")
            if plan.status != "awaiting_confirmation":
                raise DomainError("PLAN_STATE_INVALID")
            receipt_nodes = set(json.loads(receipt.node_ids_json))
            records = list(session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id)).all())
            action_json = json.dumps(sorted((json.loads(item.args_json) for item in records), key=lambda item: item["id"]), ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":"))
            expected_action_hash = "sha256:" + hashlib.sha256(action_json.encode("utf-8")).hexdigest()
            display_json = json.dumps(json.loads(plan.display_json), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected_display_hash = "sha256:" + hashlib.sha256(display_json.encode("utf-8")).hexdigest()
            if expected_action_hash != plan.action_hash or expected_action_hash != receipt.action_hash:
                raise DomainError("HASH_MISMATCH")
            if expected_display_hash != plan.display_hash or expected_display_hash != receipt.display_hash:
                raise DomainError("HASH_MISMATCH")
            consumed = {item.node_id for item in session.scalars(select(ReceiptConsumption).where(ReceiptConsumption.receipt_id == receipt_id)).all()}
            record_by_logical = {json.loads(record.args_json)["id"]: record for record in records}
            indegree = {logical_id: 0 for logical_id in record_by_logical}
            children: dict[str, list[str]] = {logical_id: [] for logical_id in record_by_logical}
            for logical_id, record in record_by_logical.items():
                for dependency in json.loads(record.args_json).get("depends_on", []):
                    if dependency not in record_by_logical:
                        raise DomainError("PLAN_DEPENDENCY_MISSING")
                    indegree[logical_id] += 1
                    children[dependency].append(logical_id)
            ready = [logical_id for logical_id, degree in indegree.items() if degree == 0]
            topo_order: list[str] = []
            while ready:
                current = ready.pop(0)
                topo_order.append(current)
                for child in children[current]:
                    indegree[child] -= 1
                    if indegree[child] == 0:
                        ready.append(child)
            if len(topo_order) != len(record_by_logical):
                raise DomainError("PLAN_CYCLE")
            if receipt_nodes != set(record_by_logical):
                raise DomainError("RECEIPT_NODE_SET_MISMATCH")
            records = [record_by_logical[logical_id] for logical_id in topo_order]
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.status != "awaiting_confirmation":
                raise DomainError("PLAN_STATE_INVALID")
            plan.status = "running"
        capabilities = CapabilityService(self.database)
        reservation_id: str | None = None
        results: list[dict[str, Any]] = []
        for record in records:
            data = json.loads(record.args_json)
            logical_id = data["id"]
            if logical_id in consumed:
                continue
            if any(dependency not in consumed and dependency not in {item["node_id"] for item in results}
                   for dependency in data.get("depends_on", [])):
                raise DomainError("PLAN_DEPENDENCY_NOT_READY")
            with self.database.sessions.begin() as session:
                current = session.get(PlanNodeRecord, record.id)
                if current is not None:
                    current.status = "ready"
                    current.status = "running"
            try:
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
            except DomainError as error:
                status, result_data = "failed", {"error_code": error.code}
            with self.database.sessions.begin() as session:
                current = session.get(PlanNodeRecord, record.id)
                if current is not None:
                    current.status = status
                if status in {"succeeded", "scheduled", "reserved"}:
                    session.add(ReceiptConsumption(receipt_id=receipt_id, node_id=logical_id,
                                                   operation_id=f"{plan_id}-{logical_id}", consumed_at=datetime.now(timezone.utc).replace(tzinfo=None)))
            results.append({"node_id": data["id"], "status": status, "data": result_data})
            if status not in {"succeeded", "scheduled", "reserved"}:
                break
        with self.database.sessions() as session:
            current_nodes = session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id)).all()
            node_statuses = [node.status for node in current_nodes]
        final_status = "succeeded" if all(status in {"succeeded", "scheduled", "reserved"} for status in node_statuses) else "partially_completed"
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is not None:
                plan.status = final_status
            receipt = session.get(ReceiptRecord, receipt_id)
            if receipt is not None and final_status == "succeeded":
                receipt.status = "consumed"
        return {"plan_id": plan_id, "status": final_status, "nodes": results}

    def pause(self, user_id: str, plan_id: str) -> dict[str, str]:
        return self._set_status(user_id, plan_id, "paused", {"awaiting_confirmation", "running"})

    def cancel(self, user_id: str, plan_id: str) -> dict[str, str]:
        return self._set_status(user_id, plan_id, "cancelled", {"draft", "prepared", "awaiting_confirmation", "paused"})

    def resume(self, user_id: str, plan_id: str) -> dict[str, str]:
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if plan.status not in {"paused", "partially_completed"}:
                raise DomainError("PLAN_STATE_INVALID")
            failed_nodes = session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id,
                                                                       PlanNodeRecord.status == "failed")).all()
            for node in failed_nodes:
                node.status = "pending"
            plan.status = "awaiting_confirmation"
            return {"plan_id": plan_id, "status": plan.status}

    def _set_status(self, user_id: str, plan_id: str, target: str, allowed: set[str]) -> dict[str, str]:
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if plan.status not in allowed:
                raise DomainError("PLAN_STATE_INVALID")
            plan.status = target
            return {"plan_id": plan_id, "status": target}
