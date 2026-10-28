"""Static capability registry consumed by planners and plan validation."""

from __future__ import annotations

from typing import Any

CAPABILITIES = {
    "Q01": "account.balance", "Q02": "transactions.query", "T01": "transfer.create", "T02": "transfer.create",
    "T03": "transfer.create", "T04": "schedule.create", "T05": "aa.create", "B01": "analysis.run",
    "B02": "analysis.detect_anomalies", "B03": "reports.generate", "B04": "reports.generate",
    "W01": "products.recommend", "W02": "products.compare", "W03": "risk.submit", "W04": "investment.subscribe",
    "W05": "investment.redeem", "W06": "investment.positions", "C01": "card.apply", "C02": "card.limit.update",
    "C03": "card.restrictions.update", "C04": "card.restrictions.update", "C05": "card.loss.report",
    "C06": "card.loss.release", "C07": "card.freeze", "C08": "security.pin.change", "S01": "subscriptions.list",
    "S02": "subscriptions.reminder.set", "S03": "subscriptions.cancel", "X01": "events.lookup",
    "X02": "funds.reserve", "X03": "merchant.order.create", "X04": "merchant.order.create", "X05": "card.freeze",
}

TOOL_REGISTRY: dict[str, dict[str, Any]] = {}
for capability_id, tool_name in CAPABILITIES.items():
    TOOL_REGISTRY[capability_id] = {
        "tool": tool_name,
        "version": "1.0",
        "preconditions": ["resource_owner"] if "." in tool_name else [],
        "security": {"minimum_assurance": "none" if capability_id.startswith(("Q", "B", "W0", "X01")) else "confirm"},
        "compensation": None if capability_id.startswith(("Q", "B", "W0", "S01", "X01")) else "explicit_command",
        "card": {"title": capability_id, "show_evidence": True},
    }
