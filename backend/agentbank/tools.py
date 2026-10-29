"""Static capability registry consumed by planners and plan validation."""

from __future__ import annotations

from typing import Any

from agentbank.errors import DomainError

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
READ_ONLY = {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "S01", "X01"}
PROTECTIVE = {"C02", "C03", "C07", "X02"}
MFA = {"W04", "W05", "C01", "C04", "C05", "C06", "C08", "X05"}
for capability_id, tool_name in CAPABILITIES.items():
    assurance = "none" if capability_id in READ_ONLY else "mfa" if capability_id in MFA else "confirm"
    TOOL_REGISTRY[capability_id] = {
        "tool": tool_name,
        "version": "1.0",
        "kind": "query" if capability_id in READ_ONLY else "command",
        "preconditions": ["resource_owner"] if "." in tool_name else [],
        "security": {"minimum_assurance": assurance, "protective": capability_id in PROTECTIVE},
        "compensation": None if capability_id in READ_ONLY else "explicit_command",
        "card": {"title": capability_id, "show_evidence": True},
    }


INPUT_FIELDS: dict[str, set[str]] = {
    "Q01": set(), "Q02": {"start_date", "end_date", "category", "limit"},
    "T01": {"recipient_name", "amount_minor", "memo"},
    "T02": {"phone", "amount_minor", "memo"}, "T03": {"alias", "recipient_name", "amount_minor", "memo"},
    "T04": {"recipient_id", "recipient_name", "amount_minor", "max_uses", "period", "next_due"},
    "T05": {"action", "total_minor", "participant_ids", "bill_id"},
    "B01": {"dsl"}, "B02": set(), "B03": {"start_month", "end_month"}, "B04": {"year", "start_month", "end_month"},
    "W01": set(), "W02": {"product_ids"}, "W03": {"answers"},
    "W04": {"product_id", "amount_minor", "assurance", "device_token", "otp"},
    "W05": {"order_id", "product_id", "assurance", "device_token", "otp"}, "W06": set(),
    "C01": {"virtual", "assurance", "device_token", "otp"},
    "C02": {"card_id", "limit_minor", "assurance", "device_token", "otp"},
    "C03": {"card_id", "restriction"},
    "C04": {"card_id", "restriction", "assurance", "device_token", "otp"},
    "C05": {"card_id", "assurance", "device_token", "otp"},
    "C06": {"card_id", "assurance", "device_token", "otp"},
    "C07": {"card_id", "action", "assurance", "device_token", "otp"},
    "C08": {"card_id", "new_pin", "assurance", "device_token", "otp"},
    "S01": {"subscription_id"}, "S02": {"subscription_id", "days"}, "S03": {"subscription_id"},
    "X01": set(), "X02": {"amount_minor", "plan_id"},
    "X03": {"delivery_date", "dispatch_on", "amount_minor", "reservation_id", "simulate_failure"},
    "X04": {"delivery_date", "dispatch_on", "amount_minor", "reservation_id", "simulate_failure"},
    "X05": {"card_id", "assurance", "device_token", "otp"},
}

STRICT_INTEGER_FIELDS = {"amount_minor", "total_minor", "limit_minor", "max_uses", "days", "limit"}
STRING_FIELDS = {"recipient_name", "phone", "alias", "memo", "period", "next_due", "start_date", "end_date",
                 "start_month", "end_month", "year", "product_id", "order_id", "card_id", "restriction",
                 "subscription_id", "bill_id", "plan_id", "delivery_date", "dispatch_on", "assurance", "device_token", "otp", "new_pin", "action"}


def validate_capability_payload(capability_id: str, payload: dict[str, Any]) -> None:
    allowed = INPUT_FIELDS.get(capability_id)
    if allowed is None:
        raise DomainError("CAPABILITY_NOT_REGISTERED")
    unknown = set(payload) - allowed
    if unknown:
        raise DomainError("CAPABILITY_INPUT_UNKNOWN_FIELD")
    for key, value in payload.items():
        if key in STRICT_INTEGER_FIELDS and (not isinstance(value, int) or isinstance(value, bool)):
            raise DomainError("CAPABILITY_INPUT_TYPE")
        if key in STRING_FIELDS and not isinstance(value, str):
            raise DomainError("CAPABILITY_INPUT_TYPE")
        if key in {"virtual", "simulate_failure"} and not isinstance(value, bool):
            raise DomainError("CAPABILITY_INPUT_TYPE")
        if key == "participant_ids" and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
            raise DomainError("CAPABILITY_INPUT_TYPE")
        if key == "answers" and (not isinstance(value, list) or len(value) != 10 or any(not isinstance(item, int) or isinstance(item, bool) for item in value)):
            raise DomainError("CAPABILITY_INPUT_TYPE")
        if key == "product_ids" and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
            raise DomainError("CAPABILITY_INPUT_TYPE")
    for key in STRICT_INTEGER_FIELDS & set(payload):
        if int(payload[key]) <= 0 and key not in {"days"}:
            raise DomainError("CAPABILITY_INPUT_RANGE")
