from __future__ import annotations

from collections import deque
import re
from typing import Any

from agentbank.capability_contracts import CAPABILITY_CONTRACTS
from agentbank.errors import DomainError
from agentbank.tools import CAPABILITIES, INPUT_FIELDS, TOOL_REGISTRY, validate_capability_payload

_REQUIRED_PLAN_ARGUMENTS: dict[str, set[str]] = {
    "Q02": set(), "T01": {"amount_minor"}, "T02": {"phone", "amount_minor"},
    "T03": {"amount_minor"}, "T04": {"amount_minor"}, "T05": {"total_minor", "participant_ids"},
    "B01": {"dsl"}, "B03": {"start_month", "end_month"},
    "B04": {"start_month", "end_month"}, "W02": {"product_ids"}, "W03": {"answers"},
    "W04": {"product_id", "amount_minor"}, "W05": {"order_id"},
    "C01": {"virtual"}, "C02": {"limit_minor"}, "C03": {"restriction"},
    "C04": {"restriction"}, "C05": {"card_id"}, "C06": {"card_id"},
    "C07": {"action"}, "S02": {"subscription_id", "days"}, "S03": {"subscription_id"},
    "X02": {"amount_minor"}, "X03": {"delivery_date"}, "X04": {"delivery_date"},
    "X05": {"card_id"},
}


class PlanValidator:
    def __init__(self, registry: dict[str, dict[str, Any]], *, max_nodes: int = 20) -> None:
        self.registry = registry
        self.max_nodes = max_nodes

    @classmethod
    def validate_proposal(cls, nodes: list[dict[str, Any]], *, max_nodes: int = 20) -> list[str]:
        """Validate model-proposed capability nodes before server-side compilation."""
        registry = {
            capability_id: {"kind": TOOL_REGISTRY[capability_id]["kind"],
                            "compensation": CAPABILITY_CONTRACTS[capability_id].compensation}
            for capability_id in CAPABILITIES
        }
        normalized: list[dict[str, Any]] = []
        for index, node in enumerate(nodes):
            capability_id = str(node.get("tool", ""))
            definition = registry.get(capability_id)
            if definition is None:
                # Let the canonical validator return the stable unknown-tool code.
                normalized.append(dict(node))
                continue
            candidate = dict(node)
            candidate.setdefault("idempotency_key", f"model-{index}")
            if definition.get("kind") == "command":
                candidate.setdefault("compensation", CAPABILITY_CONTRACTS[capability_id].compensation)
            normalized.append(candidate)
        order = cls(registry, max_nodes=max_nodes).validate(normalized)
        for node in normalized:
            capability_id = str(node["tool"])
            if capability_id == "C08":
                raise DomainError("PLAN_CAPABILITY_NOT_ALLOWED")
            args = node.get("args", {})
            if set(args) - INPUT_FIELDS[capability_id]:
                raise DomainError("PLAN_ARGUMENT_INVALID")
            missing = _REQUIRED_PLAN_ARGUMENTS.get(capability_id, set()) - set(args)
            if missing:
                raise DomainError("PLAN_ARGUMENT_MISSING")
            if capability_id in {"T01", "T03", "T04"} and not (set(args) & {"recipient_id", "recipient_name", "alias"}):
                raise DomainError("PLAN_ARGUMENT_MISSING")
            try:
                validate_capability_payload(capability_id, cls._replace_refs(args))
            except DomainError as error:
                raise DomainError("PLAN_ARGUMENT_INVALID") from error
            expected_compensation = CAPABILITY_CONTRACTS[capability_id].compensation
            if (TOOL_REGISTRY[capability_id]["kind"] == "command"
                    and node.get("compensation") != expected_compensation):
                raise DomainError("COMPENSATION_INVALID")
        return order

    @staticmethod
    def _replace_refs(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                return "__reference__"
            return {key: PlanValidator._replace_refs(child) for key, child in value.items()}
        if isinstance(value, list):
            return [PlanValidator._replace_refs(child) for child in value]
        return value

    def validate(self, nodes: list[dict[str, Any]]) -> list[str]:
        if not nodes or len(nodes) > self.max_nodes:
            raise DomainError("PLAN_TOO_LARGE")
        ids = [str(node.get("id", "")) for node in nodes]
        if len(set(ids)) != len(ids) or any(not node_id for node_id in ids):
            raise DomainError("PLAN_NODE_INVALID")
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", node_id) for node_id in ids):
            raise DomainError("PLAN_NODE_INVALID")
        node_map = dict(zip(ids, nodes, strict=True))
        indegree = {node_id: 0 for node_id in ids}
        edges: dict[str, list[str]] = {node_id: [] for node_id in ids}
        for node in nodes:
            tool = str(node.get("tool", ""))
            definition = self.registry.get(tool)
            if definition is None:
                raise DomainError("TOOL_NOT_REGISTERED")
            depends = list(node.get("depends_on", []))
            if len(set(depends)) != len(depends) or str(node["id"]) in depends:
                raise DomainError("PLAN_DEPENDENCY_INVALID")
            for dependency in depends:
                if dependency not in node_map:
                    raise DomainError("PLAN_DEPENDENCY_MISSING")
                indegree[str(node["id"])] += 1
                edges[str(dependency)].append(str(node["id"]))
            if definition.get("kind") == "command":
                self._reject_untrusted(node.get("args", {}))
            if not node.get("idempotency_key"):
                raise DomainError("IDEMPOTENCY_REQUIRED")
            if not node.get("compensation") and definition.get("kind") == "command":
                raise DomainError("COMPENSATION_REQUIRED")
            if (isinstance(node.get("timeout_ms", 5_000), bool)
                    or not isinstance(node.get("timeout_ms", 5_000), int)
                    or not 0 < node.get("timeout_ms", 5_000) <= 120_000):
                raise DomainError("TIMEOUT_REQUIRED")
            if not isinstance(node.get("idempotency_key"), str) or len(node["idempotency_key"]) > 160:
                raise DomainError("IDEMPOTENCY_REQUIRED")
        queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
        order: list[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for child in edges[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(order) != len(nodes):
            raise DomainError("PLAN_CYCLE")
        positions = {node_id: index for index, node_id in enumerate(order)}
        for node in nodes:
            for target in self._refs(node.get("args", {})):
                target_node = target.split(".", 1)[0]
                if target_node not in node_map:
                    raise DomainError("REF_TARGET_MISSING")
                if positions[target_node] >= positions[str(node["id"])] or target_node not in node.get("depends_on", []):
                    raise DomainError("REF_DEPENDENCY_REQUIRED")
        return order

    def _reject_untrusted(self, value: Any) -> None:
        if isinstance(value, dict):
            if value.get("source") == "untrusted_field":
                raise DomainError("UNTRUSTED_WRITE_ARGUMENT")
            for child in value.values():
                self._reject_untrusted(child)
        elif isinstance(value, list):
            for child in value:
                self._reject_untrusted(child)

    def _refs(self, value: Any) -> list[str]:
        refs: list[str] = []
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str):
                refs.append(ref)
            for child in value.values():
                refs.extend(self._refs(child))
        elif isinstance(value, list):
            for child in value:
                refs.extend(self._refs(child))
        return refs
