from __future__ import annotations

import json
import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from agentbank.contracts import Plan, PlanNode
from agentbank.db import (Account, AuditLog, CapabilityRecord, Database, DecisionRecord, Event, LedgerEntry,
                          Operation, PlanNodeRecord, PlanRecord, ReceiptConsumption, ReceiptRecord, Reservation)
from agentbank.errors import DomainError
from agentbank.planning.validator import PlanValidator
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.auth.devices import DeviceService
from agentbank.capability_contracts import CAPABILITY_CONTRACTS
from agentbank.tools import CAPABILITIES, INPUT_FIELDS, READ_ONLY, TOOL_REGISTRY, validate_capability_payload


COMPOSITE_PLAN_TEMPLATES: dict[str, dict[str, Any]] = {
    "query_analysis_report": {"required_slots": {"start_month", "end_month", "group_by", "report"},
                              "allowed_slots": {"start_month", "end_month", "group_by", "report", "category"}},
    "investment_review": {"required_slots": set(), "allowed_slots": set()},
}
READ_ONLY_CAPABILITIES = {"Q02", "B01", "B03", "B04", "W01", "W02"}
PLAN_READ_ONLY = READ_ONLY - {"S01"}


class BusinessPlanService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.registry = {
            "events.lookup": {"kind": "query"}, "funds.reserve": {"kind": "command"},
            "merchant.order.create": {"kind": "command"}, "funds.release": {"kind": "command"},
        }

    def create_proposed_plan(self, user_id: str, intent: str, proposed_nodes: list[PlanNode]) -> Plan:
        if not proposed_nodes:
            raise DomainError("PLAN_TOO_LARGE")
        raw_nodes = [node.model_dump(mode="json") for node in proposed_nodes]
        proposal_order = PlanValidator.validate_proposal(raw_nodes)
        plan_id = f"plan_{uuid.uuid4().hex}"
        provisional = {
            node.id: PlanNode(id=node.id, tool=node.tool, args=dict(node.args),
                              depends_on=list(node.depends_on), compensation=node.compensation,
                              timeout_ms=node.timeout_ms, idempotency_key=f"{plan_id}-{node.id}")
            for node in proposed_nodes
        }
        preflight: dict[str, dict[str, Any]] = {}
        read_nodes = {node_id for node_id, node in provisional.items() if node.tool in PLAN_READ_ONLY}
        capabilities = CapabilityService(self.database)
        for node_id in proposal_order:
            node = provisional[node_id]
            if node_id not in read_nodes:
                continue
            payload = self._resolve_preflight_refs(node.args, preflight, read_nodes, require_resolved=True)
            result = capabilities.execute(user_id, node.tool, payload, node.idempotency_key)
            if result.status != "succeeded":
                raise DomainError("PLAN_PREPARE_FAILED")
            preflight[node_id] = result.data

        compiled: list[PlanNode] = []
        for node in proposed_nodes:
            capability_id = node.tool
            args = self._resolve_preflight_refs(provisional[node.id].args, preflight, read_nodes,
                                                require_resolved=False)
            if capability_id in {"X03", "X04"}:
                item = "flower" if capability_id == "X03" else "cake"
                delivery_date = str(args.get("delivery_date", self._business_date()))
                quote = capabilities.quote_merchant(
                    user_id, item, delivery_date,
                    amount_minor=int(args["amount_minor"]) if "amount_minor" in args else None)
                args.update({"quote_id": quote["quote_id"], "amount_minor": quote["amount_minor"],
                             "delivery_date": quote["delivery_date"]})
                if "dispatch_on" not in args:
                    args["dispatch_on"] = (date.fromisoformat(delivery_date) - timedelta(days=2)).isoformat()
            if capability_id == "X02":
                args["plan_id"] = plan_id
            compiled.append(PlanNode(id=node.id, tool=capability_id, args=args,
                                     depends_on=list(node.depends_on),
                                     compensation=CAPABILITY_CONTRACTS[capability_id].compensation,
                                     timeout_ms=provisional[node.id].timeout_ms,
                                     idempotency_key=provisional[node.id].idempotency_key))
        compiled_nodes = [node.model_dump(mode="json") for node in compiled]
        PlanValidator.validate_proposal(compiled_nodes, allow_server_owned=True)
        display = self._plan_display(compiled)
        display_json = json.dumps(display, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        action_json = json.dumps(sorted(compiled_nodes, key=lambda item: item["id"]),
                                  ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        display_hash = "sha256:" + hashlib.sha256(display_json.encode("utf-8")).hexdigest()
        action_hash = "sha256:" + hashlib.sha256(action_json.encode("utf-8")).hexdigest()
        required = max((self._assurance(node.tool, node.args) for node in compiled),
                       key=lambda value: {"none": 0, "confirm": 1, "mfa": 2}[value])
        created = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.database.sessions.begin() as session:
            session.add(PlanRecord(id=plan_id, user_id=user_id, revision=1, status="awaiting_confirmation",
                                   display_hash=display_hash, action_hash=action_hash,
                                   display_json=display_json, created_at=created))
            session.add(ReceiptRecord(id=f"rcp_{uuid.uuid4().hex}", subject_id=user_id, plan_id=plan_id,
                                      plan_revision=1, node_ids_json=json.dumps([node.id for node in compiled]),
                                      action_hash=action_hash, display_hash=display_hash, assurance=required,
                                      issued_at=created, expires_at=created + timedelta(minutes=5), status="pending"))
            for node in compiled:
                session.add(PlanNodeRecord(id=f"{plan_id}-{node.id}", plan_id=plan_id, tool=node.tool,
                                           args_json=json.dumps(node.model_dump(mode="json"), ensure_ascii=False,
                                                                sort_keys=True), status="pending",
                                           idempotency_key=node.idempotency_key))
        return Plan(plan_id=plan_id, revision=1, status="awaiting_confirmation", user_id=user_id,
                    nodes=compiled, display=display, created_at=created)

    @classmethod
    def _resolve_preflight_refs(cls, value: Any, observations: dict[str, dict[str, Any]],
                                read_nodes: set[str], *, require_resolved: bool) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                path = value["$ref"]
                if not isinstance(path, str):
                    raise DomainError("PLAN_REFERENCE_INVALID")
                node_id, *fields = path.split(".")
                if node_id not in read_nodes:
                    if require_resolved:
                        raise DomainError("PLAN_REFERENCE_NOT_PREPARABLE")
                    return dict(value)
                current: Any = observations.get(node_id)
                if current is None:
                    raise DomainError("REF_TARGET_MISSING")
                for field in fields:
                    if not isinstance(current, dict) or field not in current:
                        raise DomainError("REF_FIELD_MISSING")
                    current = current[field]
                return current
            return {key: cls._resolve_preflight_refs(child, observations, read_nodes,
                                                     require_resolved=require_resolved)
                    for key, child in value.items()}
        if isinstance(value, list):
            return [cls._resolve_preflight_refs(child, observations, read_nodes,
                                                require_resolved=require_resolved) for child in value]
        return value

    @staticmethod
    def _business_date() -> str:
        return date.today().isoformat()

    @staticmethod
    def _assurance(capability_id: str, args: dict[str, Any]) -> str:
        if capability_id == "C07" and args.get("action") == "unfreeze":
            return "mfa"
        if capability_id == "C02":
            # The service contract is confirm by default; raising a user's
            # current limit requires strong verification.
            return "confirm"
        return str(TOOL_REGISTRY[capability_id]["security"]["minimum_assurance"])

    @classmethod
    def _plan_display(cls, nodes: list[PlanNode]) -> list[dict[str, str]]:
        display: list[dict[str, str]] = []
        labels = {"Q01": "查询本人余额", "Q02": "查询本人交易", "B01": "分析本人交易",
                  "B02": "检查异常交易", "B03": "生成月报", "B04": "生成年报",
                  "W01": "查询理财推荐", "W02": "比较理财产品", "W06": "查询理财持仓",
                  "X01": "查询本人事件", "X02": "预留资金", "X03": "创建鲜花订单",
                  "X04": "创建蛋糕订单"}
        for node in nodes:
            display.append({"label": labels.get(node.tool, node.tool), "value": node.id})
            for key, label in (("amount_minor", "金额"), ("total_minor", "总金额"),
                               ("limit_minor", "额度"), ("recipient_name", "收款人"),
                               ("event_id", "事件"), ("event_date", "日期"),
                               ("delivery_date", "送达日期"), ("dispatch_on", "配送日期"),
                               ("product_id", "理财产品"), ("subscription_id", "订阅"),
                               ("card_id", "银行卡"), ("restriction", "限制项"),
                               ("action", "操作")):
                if key in node.args:
                    value = node.args[key]
                    if key.endswith("_minor") and isinstance(value, (int, float)):
                        value = f"¥{int(value) / 100:.2f}"
                    display.append({"label": f"{node.id}·{label}", "value": str(value)})
        return display

    def create_composite_plan(self, user_id: str, template_id: str, slots: dict[str, Any]) -> Plan:
        template = COMPOSITE_PLAN_TEMPLATES.get(template_id)
        if template is None:
            raise DomainError("PLAN_TEMPLATE_NOT_FOUND")
        if not isinstance(slots, dict) or template["required_slots"] - slots.keys():
            raise DomainError("PLAN_SLOTS_REQUIRED")
        if slots.keys() - template["allowed_slots"]:
            raise DomainError("PLAN_SLOT_INVALID")
        plan_id = f"plan_{uuid.uuid4().hex}"
        if template_id == "query_analysis_report":
            start_month, end_month = str(slots["start_month"]), str(slots["end_month"])
            try:
                start_value = date.fromisoformat(f"{start_month}-01")
                end_value = date.fromisoformat(f"{end_month}-01")
                end_date = date(end_value.year, end_value.month,
                                (date(end_value.year + (end_value.month == 12),
                                      1 if end_value.month == 12 else end_value.month + 1, 1)
                                 - timedelta(days=1)).day).isoformat()
            except ValueError as error:
                raise DomainError("PLAN_SLOT_INVALID") from error
            if start_value > end_value:
                raise DomainError("PLAN_SLOT_INVALID")
            report = slots["report"]
            if report not in {"monthly", "yearly"}:
                raise DomainError("PLAN_SLOT_INVALID")
            group_by = slots["group_by"]
            if not isinstance(group_by, list) or not group_by:
                raise DomainError("PLAN_SLOT_INVALID")
            query_payload: dict[str, Any] = {"start_date": start_value.isoformat(), "end_date": end_date}
            if slots.get("category") is not None:
                query_payload["category"] = str(slots["category"])
            nodes = [
                PlanNode(id="transactions", tool="Q02", args=query_payload,
                         idempotency_key=f"{plan_id}-transactions"),
                PlanNode(id="analysis", tool="B01", args={"dsl": {"source": "transactions",
                         "group_by": group_by}}, depends_on=["transactions"],
                         idempotency_key=f"{plan_id}-analysis"),
            ]
            if report == "monthly":
                nodes.append(PlanNode(id="report", tool="B03", args={"start_month": start_month,
                                 "end_month": end_month}, depends_on=["analysis"],
                                 idempotency_key=f"{plan_id}-report"))
            else:
                nodes.append(PlanNode(id="report", tool="B04", args={"year": start_month[:4],
                                 "start_month": start_month, "end_month": end_month},
                                 depends_on=["analysis"], idempotency_key=f"{plan_id}-report"))
        else:
            nodes = [PlanNode(id="recommendations", tool="W01", args={},
                              idempotency_key=f"{plan_id}-recommendations"),
                     PlanNode(id="products", tool="W02", args={"recommendation_node": "recommendations"},
                              depends_on=["recommendations"], idempotency_key=f"{plan_id}-products")]
        registry = {capability_id: {"kind": "query"} for capability_id in READ_ONLY_CAPABILITIES}
        PlanValidator(registry).validate([node.model_dump() for node in nodes])
        for node in nodes:
            if node.tool in READ_ONLY_CAPABILITIES:
                payload = {key: value for key, value in node.args.items() if key != "recommendation_node"}
                if node.tool == "W02":
                    payload = {}
                validate_capability_payload(node.tool, payload)
        created = datetime.now(timezone.utc).replace(tzinfo=None)
        display = [{"label": "只读计划模板", "value": template_id},
                   {"label": "查询节点", "value": "、".join(node.id for node in nodes)}]
        display_json = json.dumps(display, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        action_json = json.dumps([node.model_dump(mode="json") for node in sorted(nodes, key=lambda item: item.id)],
                                 ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        display_hash = "sha256:" + hashlib.sha256(display_json.encode("utf-8")).hexdigest()
        action_hash = "sha256:" + hashlib.sha256(action_json.encode("utf-8")).hexdigest()
        with self.database.sessions.begin() as session:
            session.add(PlanRecord(id=plan_id, user_id=user_id, revision=1, status="awaiting_confirmation",
                                   display_hash=display_hash, action_hash=action_hash,
                                   display_json=display_json, created_at=created))
            session.add(ReceiptRecord(id=f"rcp_{uuid.uuid4().hex}", subject_id=user_id, plan_id=plan_id,
                                      plan_revision=1, node_ids_json=json.dumps([node.id for node in nodes]),
                                      action_hash=action_hash, display_hash=display_hash, assurance="none",
                                      issued_at=created, expires_at=created + timedelta(minutes=5), status="pending"))
            for node in nodes:
                session.add(PlanNodeRecord(id=f"{plan_id}-{node.id}", plan_id=plan_id, tool=node.tool,
                                           args_json=json.dumps(node.model_dump(), ensure_ascii=False,
                                                                sort_keys=True), status="pending",
                                           idempotency_key=node.idempotency_key))
        return Plan(plan_id=plan_id, revision=1, status="awaiting_confirmation", user_id=user_id,
                    nodes=nodes, display=display, created_at=created)

    def create_birthday_plan(self, user_id: str, event_id: str, reserve_minor: int,
                             planned_event_date: str | None = None, *, items: list[str] | None = None) -> Plan:
        if reserve_minor <= 0:
            raise DomainError("PLAN_AMOUNT_INVALID")
        with self.database.sessions() as session:
            event = session.get(Event, event_id)
            if event is None or event.user_id != user_id:
                raise DomainError("EVENT_NOT_FOUND")
            event_date = planned_event_date or event.event_date
        try:
            event_date = date.fromisoformat(event_date).isoformat()
        except ValueError as error:
            raise DomainError("EVENT_DATE_INVALID") from error
        dispatch_on = (datetime.fromisoformat(event_date).date() - timedelta(days=2)).isoformat()
        plan_id = f"plan_{uuid.uuid4().hex}"
        capabilities = CapabilityService(self.database, require_device_mfa=True, enforce_prepare=False)
        selected = ["flower", "cake"] if items is None else items
        if not selected or any(item not in {"flower", "cake"} for item in selected) or len(set(selected)) != len(selected):
            raise DomainError("PLAN_ITEMS_INVALID")
        quotes = {item: capabilities.quote_merchant(user_id, item, event_date) for item in selected}
        nodes = [PlanNode(id="event", tool="events.lookup", args={"event_id": event_id, "event_date": event_date},
                          idempotency_key=f"{plan_id}-event")]
        for item, quote in quotes.items():
            nodes.append(PlanNode(id=f"quote_{item}", tool="merchant.quote",
                args={"quote_id": quote["quote_id"], "item": item, "amount_minor": quote["amount_minor"],
                      "delivery_date": event_date, "price_version": quote["price_version"]},
                depends_on=["event"], idempotency_key=f"{plan_id}-quote-{item}"))
        nodes.append(PlanNode(id="reserve", tool="funds.reserve",
            args={"amount_minor": reserve_minor, "event_id": event_id},
            depends_on=[f"quote_{item}" for item in selected], compensation="funds.release",
            idempotency_key=f"{plan_id}-reserve"))
        for item, quote in quotes.items():
            node_id = "flowers" if item == "flower" else "cake"
            nodes.append(PlanNode(id=node_id, tool="merchant.order.create",
                args={"item": item, "event_id": event_id, "quote_id": quote["quote_id"],
                      "amount_minor": quote["amount_minor"], "delivery_date": event_date, "dispatch_on": dispatch_on},
                depends_on=["reserve", f"quote_{item}"], compensation="funds.release",
                idempotency_key=f"{plan_id}-{node_id}"))
        PlanValidator({**self.registry, "merchant.quote": {"kind": "query"}}).validate(
            [node.model_dump() for node in nodes])
        created = datetime.now(timezone.utc).replace(tzinfo=None)
        item_labels = {"flower": "鲜花", "cake": "蛋糕"}
        display = [{"label": "活动", "value": event_id}, {"label": "活动日期", "value": event_date},
                   {"label": "下单日期", "value": dispatch_on}]
        display.extend({"label": f"{item_labels[item]}报价", "value": f"¥{quote['amount_minor'] / 100:.2f}"}
                       for item, quote in quotes.items())
        display.extend([{"label": "资金预留（分）", "value": str(reserve_minor)},
                        {"label": "计划订单", "value": "、".join(item_labels[item] for item in selected)}])
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
            receipts = session.scalars(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan_id,
                                                                    ReceiptRecord.subject_id == user_id)).all()
            decisions = session.scalars(select(DecisionRecord).where(DecisionRecord.plan_id == plan_id)).all()
            consumptions = session.scalars(select(ReceiptConsumption).where(
                ReceiptConsumption.receipt_id.in_([receipt.id for receipt in receipts])
            )).all() if receipts else []
            consumption_by_node = {item.node_id: item for item in consumptions}
            receipt_by_node = {node_id: receipt for receipt in receipts
                               for node_id in json.loads(receipt.node_ids_json)}
            decision_by_operation = {item.operation_id: item for item in decisions}
            nodes: list[dict[str, Any]] = []
            for record in records:
                data = json.loads(record.args_json)
                logical_id = data["id"]
                receipt = receipt_by_node.get(logical_id)
                consumption = consumption_by_node.get(logical_id)
                capability_record = session.scalar(select(CapabilityRecord).where(
                    CapabilityRecord.user_id == user_id,
                    CapabilityRecord.idempotency_key == record.idempotency_key))
                operation = session.scalar(select(Operation).where(
                    Operation.user_id == user_id,
                    Operation.idempotency_key == record.idempotency_key))
                if operation is None and consumption is not None:
                    operation = session.get(Operation, consumption.operation_id)
                    if operation is not None and operation.user_id != user_id:
                        operation = None
                if operation is not None:
                    ledger_ids = list(session.scalars(select(LedgerEntry.id).where(
                        LedgerEntry.operation_id == operation.id)).all())
                else:
                    ledger_ids = []
                trace_ids = {f"trace_{plan_id}", f"trace_{record.id}"}
                if capability_record is not None:
                    trace_ids.add(f"trace_{capability_record.id}")
                if operation is not None:
                    trace_ids.add(f"trace_{operation.id}")
                audit_events = session.scalars(select(AuditLog).where(
                    AuditLog.subject_id == user_id, AuditLog.trace_id.in_(trace_ids)
                ).order_by(AuditLog.seq)).all() if trace_ids else []
                decision = decision_by_operation.get(operation.id) if operation is not None else None
                decision_chain: list[dict[str, Any]] = []
                if decision is not None:
                    decision_chain.append({"stage": "policy", "status": "recorded",
                                          "decision_id": decision.id, "tier": decision.tier_display,
                                          "assurance": decision.assurance,
                                          "reason_codes": json.loads(decision.reason_codes_json)})
                if receipt is not None:
                    decision_chain.append({"stage": "authorization",
                                          "status": "consumed" if consumption is not None else receipt.status,
                                          "receipt_id": receipt.id})
                node_evidence = {
                    "receipt_id": receipt.id if receipt else None,
                    "receipt_status": receipt.status if receipt else None,
                    "operation_id": operation.id if operation else (consumption.operation_id if consumption else None),
                    "operation_status": operation.status if operation else None,
                    "ledger_entry_ids": ledger_ids,
                    "audit_event_ids": [event.event_id for event in audit_events],
                    "capability_record_id": capability_record.id if capability_record else None,
                    "capability_status": capability_record.status if capability_record else None,
                }
                node_view = {"node_id": record.id, "logical_id": data["id"], "tool": record.tool,
                             "status": record.status, "depends_on": data.get("depends_on", []),
                             "authorization": receipt.assurance if receipt else "none",
                             "compensation": data.get("compensation"),
                             "decision_chain": decision_chain, "evidence": node_evidence}
                if capability_record is not None and record.tool in READ_ONLY_CAPABILITIES:
                    node_view["observation"] = {"status": capability_record.status,
                                                 "data": json.loads(capability_record.data_json)}
                nodes.append(node_view)
            return {"plan_id": plan_id, "status": plan.status, "revision": plan.revision, "nodes": nodes}

    def recover_interrupted(self) -> list[str]:
        """Persist running plans as paused after process startup for explicit user resumption."""
        recovered: list[str] = []
        with self.database.sessions.begin() as session:
            records = session.scalars(select(PlanRecord).where(PlanRecord.status == "running")).all()
            for plan in records:
                plan.status = "paused"
                recovered.append(plan.id)
        return recovered

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
            event_args = json.loads(event_node.args_json)["args"]
            event_id = event_args["event_id"]
            event_date = str(changes.get("event_date", event_args.get("event_date", "")))
            reserve_minor = int(changes.get("reserve_minor", 100_000))
            order_nodes = session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id,
                                                                      PlanNodeRecord.tool == "merchant.order.create")).all()
            items = [str(json.loads(node.args_json)["args"]["item"]) for node in order_nodes]
        if reserve_minor <= 0:
            raise DomainError("PLAN_AMOUNT_INVALID")
        try:
            event_date = date.fromisoformat(event_date).isoformat()
        except ValueError as error:
            raise DomainError("EVENT_DATE_INVALID") from error
        with self.database.sessions.begin() as session:
            current = session.get(PlanRecord, plan_id)
            if current is None:
                raise DomainError("PLAN_NOT_FOUND")
            current.status = "invalidated"
            from agentbank.db import ReceiptRecord
            for receipt in session.scalars(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan_id)).all():
                receipt.status = "invalidated"
        revised = self.create_birthday_plan(user_id, event_id, reserve_minor, event_date, items=items)
        with self.database.sessions.begin() as session:
            new_record = session.get(PlanRecord, revised.plan_id)
            new_revision = (current.revision + 1) if current else 2
            if new_record is not None:
                new_record.revision = new_revision
                for receipt in session.scalars(select(ReceiptRecord).where(ReceiptRecord.plan_id == revised.plan_id)).all():
                    receipt.plan_revision = new_revision
        return Plan(plan_id=revised.plan_id, revision=new_revision, status=revised.status, user_id=user_id,
                    nodes=revised.nodes, display=revised.display, created_at=revised.created_at)

    def confirm_plan(self, user_id: str, plan_id: str, receipt_id: str, display_hash: str) -> dict[str, Any]:
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            receipt = session.get(ReceiptRecord, receipt_id)
            if plan is None or plan.user_id != user_id or receipt is None or receipt.subject_id != user_id:
                raise DomainError("NOT_OWNER")
            if receipt.plan_id != plan_id or receipt.status != "pending":
                raise DomainError("RECEIPT_INVALID")
            if plan.status not in {"awaiting_confirmation", "partially_completed"}:
                raise DomainError("PLAN_STATE_INVALID")
            if receipt.display_hash != display_hash or plan.display_hash != display_hash:
                raise DomainError("HASH_MISMATCH")
            if receipt.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
                raise DomainError("RECEIPT_EXPIRED")
            if receipt.assurance == "mfa":
                plan.status = "awaiting_mfa"
                assurance = "mfa"
            else:
                assurance = "none"
        if assurance == "mfa":
            DeviceService(self.database).issue_otp(user_id, receipt_id, receipt.action_hash)
            return {"plan_id": plan_id, "status": "awaiting_mfa", "receipt_id": receipt_id,
                    "assurance": "mfa"}
        return self.execute_plan(user_id, plan_id, receipt_id, display_hash)

    def verify_plan(self, user_id: str, plan_id: str, otp: str) -> dict[str, Any]:
        with self.database.sessions() as session:
            plan = session.get(PlanRecord, plan_id)
            receipt = session.scalar(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan_id,
                                                                  ReceiptRecord.subject_id == user_id))
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if receipt is None or receipt.assurance != "mfa":
                raise DomainError("PLAN_MFA_NOT_REQUIRED")
            if plan.status != "awaiting_mfa":
                raise DomainError("PLAN_STATE_INVALID")
            display_hash = receipt.display_hash
        DeviceService(self.database).verify(user_id, otp, receipt.id, receipt.action_hash)
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.status != "awaiting_mfa":
                raise DomainError("PLAN_STATE_INVALID")
            plan.status = "authorized"
        return self.execute_plan(user_id, plan_id, receipt.id, display_hash, mfa_verified=True)

    def execute_plan(self, user_id: str, plan_id: str, receipt_id: str, display_hash: str,
                     *, mfa_verified: bool = False) -> dict[str, Any]:
        with self.database.sessions() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            receipt = session.get(ReceiptRecord, receipt_id)
            if receipt is None or receipt.plan_id != plan_id or receipt.subject_id != user_id:
                raise DomainError("NOT_OWNER")
            if receipt.status in {"consumed", "invalidated"}:
                raise DomainError("RECEIPT_REPLAYED")
            if receipt.assurance == "mfa" and not mfa_verified:
                raise DomainError("MFA_REQUIRED")
            if receipt.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
                raise DomainError("RECEIPT_EXPIRED")
            if receipt.plan_revision != plan.revision:
                raise DomainError("PLAN_REVISION_MISMATCH")
            if receipt.display_hash != display_hash or plan.display_hash != display_hash:
                raise DomainError("HASH_MISMATCH")
            records = list(session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id)).all())
            composite = bool(records) and all(record.tool in READ_ONLY_CAPABILITIES for record in records)
            generic = bool(records) and all(record.tool in CAPABILITIES for record in records)
            allowed_states = {"awaiting_confirmation", "running", "partially_completed"} if composite else {
                "awaiting_confirmation", "running"
            }
            if mfa_verified:
                allowed_states |= {"awaiting_mfa", "authorized", "partially_completed"}
            if plan.status not in allowed_states:
                raise DomainError("PLAN_STATE_INVALID")
            receipt_nodes = set(json.loads(receipt.node_ids_json))
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
            if generic and not composite:
                PlanValidator.validate_proposal([json.loads(record.args_json) for record in records],
                                                allow_server_owned=True)
            if composite:
                PlanValidator({tool: {"kind": "query"} for tool in READ_ONLY_CAPABILITIES}).validate(
                    [json.loads(record.args_json) for record in records])
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.status not in allowed_states:
                raise DomainError("PLAN_STATE_INVALID")
            plan.status = "running"
        capabilities = CapabilityService(self.database, require_device_mfa=True, enforce_prepare=False)
        reservation_id: str | None = None
        with self.database.sessions() as session:
            reserve_record = session.scalar(select(CapabilityRecord).where(
                CapabilityRecord.user_id == user_id,
                CapabilityRecord.idempotency_key == f"{plan_id}-reserve",
            ))
            if reserve_record is not None:
                reservation_id = str(json.loads(reserve_record.data_json).get("reservation_id", "")) or None
        results: list[dict[str, Any]] = []
        completed_statuses = {"succeeded"} if composite else {"succeeded", "scheduled", "reserved", "created"}
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
            with self.database.sessions() as session:
                existing_capability = session.scalar(select(CapabilityRecord).where(
                    CapabilityRecord.user_id == user_id,
                    CapabilityRecord.idempotency_key == record.idempotency_key))
            if (composite or generic) and existing_capability is not None:
                status = existing_capability.status
                result_data = json.loads(existing_capability.data_json)
                if status not in completed_statuses:
                    results.append({"node_id": logical_id, "status": status, "data": result_data})
                    break
                with self.database.sessions.begin() as session:
                    current = session.get(PlanNodeRecord, record.id)
                    if current is not None:
                        current.status = status
                    session.add(ReceiptConsumption(receipt_id=receipt_id, node_id=logical_id,
                                                   operation_id=existing_capability.id,
                                                   consumed_at=datetime.now(timezone.utc).replace(tzinfo=None)))
                consumed.add(logical_id)
                results.append({"node_id": logical_id, "status": status, "data": result_data})
                continue
            try:
                if data["id"] == "event":
                    status = "succeeded"
                    result_data = {"event_id": data["args"]["event_id"]}
                elif data["id"] in {"quote_flower", "quote_cake"}:
                    quote = capabilities.get_merchant_quote(user_id, str(data["args"]["quote_id"]))
                    if (quote["status"] != "active" or quote["item"] != data["args"]["item"]
                            or quote["delivery_date"] != data["args"]["delivery_date"]
                            or quote["amount_minor"] != data["args"]["amount_minor"]
                            or quote["price_version"] != data["args"]["price_version"]
                            or quote["current_price_version"] != quote["price_version"]
                            or quote["current_price_minor"] != quote["amount_minor"]
                            or quote["catalog_stock"] <= 0
                            or datetime.fromisoformat(quote["expires_at"]) <= datetime.now(timezone.utc).replace(tzinfo=None)):
                        raise DomainError("QUOTE_INVALID")
                    status, result_data = "succeeded", quote
                elif generic and not composite and record.tool in CAPABILITIES and record.tool not in {"X03", "X04"}:
                    payload = self._resolve_refs(data.get("args", {}), results)
                    if "assurance" in INPUT_FIELDS[record.tool]:
                        payload["assurance"] = "mfa" if mfa_verified else "confirm"
                    if mfa_verified:
                        payload["_mfa_verified"] = True
                    result = capabilities.execute(user_id, record.tool, payload, record.idempotency_key, trusted_execution=True)
                    status, result_data = result.status, result.data
                elif generic and not composite and record.tool in {"X03", "X04"}:
                    payload = self._resolve_refs(data.get("args", {}), results)
                    if mfa_verified:
                        payload["_mfa_verified"] = True
                    result = capabilities.execute(user_id, record.tool, payload, record.idempotency_key, trusted_execution=True)
                    status, result_data = result.status, result.data
                elif record.tool in READ_ONLY_CAPABILITIES:
                    payload = {key: value for key, value in data["args"].items()
                               if key != "recommendation_node"}
                    if record.tool == "W02":
                        source_id = str(data["args"]["recommendation_node"])
                        source_key = record_by_logical[source_id].idempotency_key
                        with self.database.sessions() as session:
                            source_record = session.scalar(select(CapabilityRecord).where(
                                CapabilityRecord.user_id == user_id,
                                CapabilityRecord.idempotency_key == source_key))
                            if source_record is None or source_record.status != "succeeded":
                                raise DomainError("PLAN_DEPENDENCY_NOT_READY")
                            recommendations = json.loads(source_record.data_json).get("products", [])
                        payload = {"product_ids": [str(item["id"]) for item in recommendations]}
                    result = capabilities.execute(user_id, record.tool, payload, record.idempotency_key, trusted_execution=True)
                    status, result_data = result.status, result.data
                elif data["id"] == "reserve":
                    result = capabilities.execute(user_id, "X02", {"amount_minor": data["args"]["amount_minor"], "plan_id": plan_id}, f"{plan_id}-reserve", trusted_execution=True)
                    reservation_id = str(result.data["reservation_id"])
                    status, result_data = result.status, result.data
                else:
                    capability_id = "X03" if data["args"]["item"] == "flower" else "X04"
                    result = capabilities.execute(user_id, capability_id,
                                                   {"delivery_date": data["args"]["delivery_date"],
                                                    "dispatch_on": data["args"]["dispatch_on"],
                                                    "quote_id": data["args"]["quote_id"],
                                                    "amount_minor": data["args"]["amount_minor"],
                                                    "reservation_id": reservation_id}, f"{plan_id}-{data['id']}", trusted_execution=True)
                    status, result_data = result.status, result.data
            except DomainError as error:
                status, result_data = "failed", {"error_code": error.code}
            with self.database.sessions.begin() as session:
                current = session.get(PlanNodeRecord, record.id)
                if current is not None:
                    current.status = status
                if status in completed_statuses:
                    session.add(ReceiptConsumption(receipt_id=receipt_id, node_id=logical_id,
                                                   operation_id=f"{plan_id}-{logical_id}", consumed_at=datetime.now(timezone.utc).replace(tzinfo=None)))
            results.append({"node_id": data["id"], "status": status, "data": result_data})
            if status not in completed_statuses:
                break
            with self.database.sessions() as session:
                current_plan = session.get(PlanRecord, plan_id)
                if current_plan is None or current_plan.status != "running":
                    break
        with self.database.sessions() as session:
            current_nodes = session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id)).all()
            node_statuses = [node.status for node in current_nodes]
        with self.database.sessions() as session:
            current_plan = session.get(PlanRecord, plan_id)
            interrupted_status = current_plan.status if current_plan and current_plan.status in {"paused", "cancelled"} else None
        final_status = interrupted_status or (
            "succeeded" if all(status in completed_statuses for status in node_statuses)
            else "partially_completed"
        )
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is not None and plan.status == "running":
                plan.status = final_status
            receipt = session.get(ReceiptRecord, receipt_id)
            if receipt is not None and final_status == "succeeded":
                receipt.status = "consumed"
        return {"plan_id": plan_id, "status": final_status, "nodes": results}

    @staticmethod
    def _resolve_refs(value: Any, results: list[dict[str, Any]]) -> Any:
        by_node = {str(item["node_id"]): item.get("data", {}) for item in results}

        def resolve(item: Any) -> Any:
            if isinstance(item, dict):
                if set(item) == {"$ref"}:
                    path = str(item["$ref"])
                    node_id, *fields = path.split(".")
                    current: Any = by_node.get(node_id)
                    if current is None:
                        raise DomainError("REF_TARGET_MISSING")
                    for field in fields:
                        if not isinstance(current, dict) or field not in current:
                            raise DomainError("REF_FIELD_MISSING")
                        current = current[field]
                    return current
                return {key: resolve(child) for key, child in item.items()}
            if isinstance(item, list):
                return [resolve(child) for child in item]
            return item

        return resolve(value)

    def pause(self, user_id: str, plan_id: str) -> dict[str, str]:
        return self._set_status(user_id, plan_id, "paused", {"awaiting_confirmation", "running"})

    def cancel(self, user_id: str, plan_id: str) -> dict[str, str]:
        with self.database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if plan.status not in {"draft", "prepared", "awaiting_confirmation", "paused"}:
                raise DomainError("PLAN_STATE_INVALID")
            reservations = session.scalars(select(Reservation).where(Reservation.user_id == user_id,
                                                                       Reservation.plan_id == plan_id,
                                                                       Reservation.status == "active")).all()
            for reservation in reservations:
                account = session.get(Account, reservation.account_id)
                if account is None:
                    raise DomainError("ACCOUNT_NOT_FOUND")
                account.available_minor += reservation.amount_minor
                reservation.status = "released"
            for receipt in session.scalars(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan_id)).all():
                receipt.status = "invalidated"
            plan.status = "cancelled"
            return {"plan_id": plan_id, "status": "cancelled"}

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
