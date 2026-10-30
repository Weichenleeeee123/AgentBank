"""Static capability registry consumed by planners and plan validation."""

from __future__ import annotations

from typing import Any

from agentbank.capability_contracts import CAPABILITY_CONTRACTS, validate_input
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
        "preconditions": CAPABILITY_CONTRACTS[capability_id].preconditions,
        "security": {"minimum_assurance": assurance, "protective": capability_id in PROTECTIVE},
        "compensation": CAPABILITY_CONTRACTS[capability_id].compensation,
        "input_schema": CAPABILITY_CONTRACTS[capability_id].json_schema()["input"],
        "result_schemas": {key: model.model_json_schema() for key, model
                           in CAPABILITY_CONTRACTS[capability_id].result_models.items()},
        "evidence": CAPABILITY_CONTRACTS[capability_id].evidence,
        "card": CAPABILITY_CONTRACTS[capability_id].card,
    }


INPUT_FIELDS: dict[str, set[str]] = {
    capability_id: set(contract.input_model.model_fields) | ({"device_token", "otp"}
    if capability_id in {"W04", "W05", "C01", "C02", "C04", "C05", "C06", "C07", "C08", "X05"}
    else set())
    for capability_id, contract in CAPABILITY_CONTRACTS.items()
}


def validate_capability_payload(capability_id: str, payload: dict[str, Any]) -> None:
    if capability_id not in INPUT_FIELDS:
        raise DomainError("CAPABILITY_NOT_REGISTERED")
    from pydantic import ValidationError

    try:
        validate_input(capability_id, payload)
    except ValidationError as error:
        if capability_id == "W03" and any(item["loc"] and item["loc"][0] == "answers"
                                          for item in error.errors()):
            raise DomainError("INVALID_RISK_QUESTIONNAIRE") from error
        if capability_id == "C02" and any(item["loc"] == ("limit_minor",)
                                          and item["type"] == "less_than_equal"
                                          for item in error.errors()):
            raise DomainError("CARD_LIMIT_EXCEEDED") from error
        message = str(error).lower()
        code = "CAPABILITY_INPUT_UNKNOWN_FIELD" if "extra_forbidden" in message else "CAPABILITY_INPUT_TYPE"
        if "valueerror" in message or "must be positive" in message or "out of range" in message:
            code = "CAPABILITY_INPUT_RANGE"
        raise DomainError(code) from error
    except (TypeError, ValueError) as error:
        raise DomainError("CAPABILITY_INPUT_RANGE") from error
