from __future__ import annotations

from collections import deque
from typing import Any

from agentbank.errors import DomainError


class PlanValidator:
    def __init__(self, registry: dict[str, dict[str, Any]], *, max_nodes: int = 20) -> None:
        self.registry = registry
        self.max_nodes = max_nodes

    def validate(self, nodes: list[dict[str, Any]]) -> list[str]:
        if not nodes or len(nodes) > self.max_nodes:
            raise DomainError("PLAN_TOO_LARGE")
        ids = [str(node.get("id", "")) for node in nodes]
        if len(set(ids)) != len(ids) or any(not node_id for node_id in ids):
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
            if int(node.get("timeout_ms", 5_000)) <= 0:
                raise DomainError("TIMEOUT_REQUIRED")
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
