"""Typed input and output contracts for the complete M2 capability registry."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, create_model, field_validator, model_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _DateInput(StrictContract):
    @model_validator(mode="before")
    @classmethod
    def no_null_inputs(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("explicit null is not an input value")
        return value

    @field_validator("start_date", "end_date", "next_due", "delivery_date", "dispatch_on", check_fields=False)
    @classmethod
    def valid_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def valid_ranges(self) -> _DateInput:
        values = self.model_dump(exclude_none=True, exclude_unset=True)
        _validate_ranges(self.__class__.__name__[:3], values)
        for left, right in (("start_date", "end_date"), ("start_month", "end_month"),
                            ("dispatch_on", "delivery_date")):
            if left in values and right in values and values[left] > values[right]:
                raise ValueError(f"{left} must not follow {right}")
        return self

    @field_validator("start_month", "end_month", check_fields=False)
    @classmethod
    def valid_month(cls, value: str | None) -> str | None:
        if value is not None:
            if len(value) != 7 or value[4] != "-":
                raise ValueError("month must use YYYY-MM")
            date.fromisoformat(value + "-01")
        return value


def _optional(annotation: Any, default: Any = None) -> tuple[Any, Any]:
    return annotation | None, default


PositiveMinor = Annotated[StrictInt, Field(gt=0)]
_COMMON_FIELDS: dict[str, tuple[Any, Any]] = {
    "recipient_id": _optional(StrictStr), "recipient_name": _optional(StrictStr),
    "category": _optional(StrictStr),
    "phone": _optional(StrictStr), "alias": _optional(StrictStr), "memo": _optional(StrictStr),
    "amount_minor": _optional(PositiveMinor, 1_000), "total_minor": _optional(PositiveMinor),
    "limit_minor": _optional(Annotated[StrictInt, Field(gt=0, le=500_000)]),
    "max_uses": _optional(Annotated[StrictInt, Field(gt=0, le=1_000)], 1),
    "days": _optional(Annotated[StrictInt, Field(ge=0, le=365)], 3),
    "limit": _optional(Annotated[StrictInt, Field(gt=0, le=50_000)], 100),
    "period": _optional(Literal["once", "daily", "weekly", "monthly", "yearly"], "monthly"),
    "next_due": _optional(StrictStr), "start_date": _optional(StrictStr), "end_date": _optional(StrictStr),
    "start_month": _optional(StrictStr), "end_month": _optional(StrictStr), "year": _optional(StrictStr),
    "product_id": _optional(StrictStr), "order_id": _optional(StrictStr), "card_id": _optional(StrictStr),
    "restriction": _optional(StrictStr), "subscription_id": _optional(StrictStr), "bill_id": _optional(StrictStr),
    "plan_id": _optional(StrictStr), "delivery_date": _optional(StrictStr), "dispatch_on": _optional(StrictStr),
    "quote_id": _optional(StrictStr),
    "action": _optional(Literal["preview", "status", "pay_share", "unfreeze", "freeze"]),
    "virtual": _optional(StrictBool), "simulate_failure": _optional(StrictBool),
    "participant_ids": _optional(Annotated[list[StrictStr], Field(min_length=2, max_length=100)]),
    "product_ids": _optional(list[StrictStr]),
    "answers": _optional(Annotated[list[Annotated[StrictInt, Field(ge=0, le=5)]],
                                    Field(min_length=10, max_length=10)]),
    "dsl": _optional(dict[str, Any]),
    "reservation_id": _optional(StrictStr), "assurance": _optional(Literal["none", "confirm", "mfa"]),
    "new_pin": _optional(StrictStr),
}

_INPUT_FIELDS: dict[str, set[str]] = {
    "Q01": set(), "Q02": {"start_date", "end_date", "category", "limit"},
    "T01": {"recipient_id", "recipient_name", "amount_minor", "memo"},
    "T02": {"phone", "amount_minor", "memo"}, "T03": {"alias", "recipient_name", "amount_minor", "memo"},
    "T04": {"recipient_id", "recipient_name", "amount_minor", "max_uses", "period", "next_due"},
    "T05": {"action", "total_minor", "participant_ids", "bill_id"},
    "B01": {"dsl", "start_month", "end_month"}, "B02": set(), "B03": {"start_month", "end_month"},
    "B04": {"year", "start_month", "end_month"}, "W01": set(), "W02": {"product_ids"},
    "W03": {"answers"}, "W04": {"product_id", "amount_minor", "assurance"},
    "W05": {"order_id", "product_id", "assurance"}, "W06": set(),
    "C01": {"virtual", "assurance"}, "C02": {"card_id", "limit_minor", "assurance"},
    "C03": {"card_id", "restriction"}, "C04": {"card_id", "restriction", "assurance"},
    "C05": {"card_id", "assurance"}, "C06": {"card_id", "assurance"},
    "C07": {"card_id", "action", "assurance"}, "C08": {"card_id", "new_pin", "assurance"},
    "S01": {"subscription_id"}, "S02": {"subscription_id", "days"}, "S03": {"subscription_id"},
    "X01": set(), "X02": {"amount_minor", "plan_id"},
    "X03": {"quote_id", "delivery_date", "dispatch_on", "amount_minor", "reservation_id", "simulate_failure"},
    "X04": {"quote_id", "delivery_date", "dispatch_on", "amount_minor", "reservation_id", "simulate_failure"},
    "X05": {"card_id", "assurance"},
}

_OTP_CAPABILITIES = {"W04", "W05", "C01", "C02", "C04", "C05", "C06", "C07", "C08", "X05"}
_AUTH_REQUIRED = {"receipt_id": StrictStr, "action_hash": StrictStr,
                  "display_hash": StrictStr, "assurance": Literal["confirm", "mfa"]}


def _model(name: str, fields: dict[str, tuple[Any, Any]]) -> type[StrictContract]:
    factory = cast(Any, create_model)
    return cast(type[StrictContract], factory(name, __base__=_DateInput, **fields))


class BusinessResult(BaseModel):
    """Known business evidence is typed; additional audit/authorization metadata survives."""

    model_config = ConfigDict(extra="allow", strict=True)


class TransactionEvidence(BusinessResult):
    txn_id: StrictStr
    user_id: StrictStr
    posted_at: StrictStr
    amount_minor: StrictInt
    merchant: StrictStr
    category: StrictStr
    direction: Literal["debit", "credit", "transfer"]


class ReportPeriod(BusinessResult):
    income_minor: StrictInt
    expense_minor: StrictInt
    transfer_minor: StrictInt
    missing: StrictBool
    txn_ids: list[StrictStr]
    yoy_change: float | None = None


class MonthEvidence(ReportPeriod):
    month: StrictStr
    mom_change: float | None = None


class YearEvidence(ReportPeriod):
    year: StrictInt


class ProductEvidence(BusinessResult):
    id: StrictStr
    name: StrictStr
    risk_level: StrictStr
    term_days: StrictInt
    fee_bps: StrictInt
    redemption_days: StrictInt


class AnomalyEvidence(BusinessResult):
    reason_code: StrictStr
    txn_ids: list[StrictStr]
    evidence: StrictStr | dict[str, Any]


class SubscriptionEvidence(BusinessResult):
    merchant: StrictStr
    status: Literal["confirmed", "suspected"]
    evidence_txn_ids: list[StrictStr]
    price_increase: StrictBool


class PositionEvidence(BusinessResult):
    product_id: StrictStr
    shares: StrictInt
    cost_minor: StrictInt
    market_value_minor: StrictInt
    pnl_minor: StrictInt
    order_ids: list[StrictStr]


class TransferEvidence(BusinessResult):
    operation_id: StrictStr
    status: StrictStr
    amount_minor: StrictInt
    ledger_entry_ids: list[StrictStr]


class DslEvidence(BusinessResult):
    user_id: StrictStr
    txn_ids: list[StrictStr]


class EventEvidence(BusinessResult):
    event_id: StrictStr
    title: StrictStr
    event_date: StrictStr


class HistoryEvidence(BusinessResult):
    category: StrictStr
    txn_ids: list[StrictStr]
    source: Literal["historical_transactions"]


class BillShareEvidence(BusinessResult):
    participant_id: StrictStr
    amount_minor: StrictInt
    status: StrictStr


def _result_model(name: str, fields: dict[str, Any]) -> type[BaseModel]:
    factory = cast(Any, create_model)
    return cast(type[BaseModel], factory(name, __base__=BusinessResult,
                                         **{key: value if isinstance(value, tuple) else (value, ...)
                                            for key, value in fields.items()}))


INPUT_MODELS: dict[str, type[StrictContract]] = {
    capability_id: _model(f"{capability_id}Input", {key: _COMMON_FIELDS[key] for key in sorted(fields)})
    for capability_id, fields in _INPUT_FIELDS.items()
}


_OUTPUT_FIELDS: dict[str, dict[str, Any]] = {
    "Q01": {"account_id": StrictStr, "ledger_balance_minor": StrictInt, "available_minor": StrictInt,
            "currency": StrictStr, "as_of": StrictStr},
    "Q02": {"transactions": list[TransactionEvidence]},
    "T01": {"transfer_result": TransferEvidence}, "T02": {"transfer_result": TransferEvidence},
    "T03": {"transfer_result": TransferEvidence},
    "T04": {"schedule_id": StrictStr, "mandate_id": StrictStr, "next_due": StrictStr,
            "amount_minor": StrictInt, "max_uses": StrictInt},
    "T05": {"bill_id": StrictStr, "shares": dict[str, StrictInt], "request_only": StrictBool},
    "B01": {"rows": list[DslEvidence], "query_hash": StrictStr, "row_count": StrictInt},
    "B02": {"anomalies": list[AnomalyEvidence]},
    "B03": {"months": list[MonthEvidence]},
    "B04": {"months": list[MonthEvidence], "years": list[YearEvidence], "year": StrictStr},
    "W01": {"products": list[ProductEvidence]}, "W02": {"products": list[ProductEvidence]},
    "W03": {"level": StrictStr, "score": StrictInt, "expires_on": StrictStr},
    "W04": {"order_id": StrictStr, "product_id": StrictStr, "status": Literal["subscribed"],
            "funds_status": Literal["debited"], "operation_id": StrictStr},
    "W05": {"order_id": StrictStr, "status": StrictStr, "expected_settlement_at": StrictStr},
    "W06": {"positions": list[PositionEvidence], "source": Literal["simulated_market"], "as_of": StrictStr},
    "C01": {"card_id": StrictStr, "status": StrictStr},
    "C02": {"card_id": StrictStr, "limit_minor": StrictInt},
    "C03": {"card_id": StrictStr, "restriction": StrictStr, "status": StrictStr},
    "C04": {"card_id": StrictStr, "restriction": StrictStr, "status": StrictStr},
    "C05": {"card_id": StrictStr, "status": StrictStr},
    "C06": {"card_id": StrictStr, "status": StrictStr},
    "C07": {"card_id": StrictStr, "status": StrictStr, "payment_blocked": StrictBool},
    "C08": {"card_id": StrictStr, "status": Literal["pin_changed"], "secret_persisted": Literal[False]},
    "S01": {"subscriptions": list[SubscriptionEvidence]},
    "S02": {"reminder_id": StrictStr, "subscription_id": StrictStr, "remind_before_days": StrictInt},
    "S03": {"subscription_id": StrictStr, "status": Literal["cancelled"], "debit_mandate": Literal["revoked"]},
    "X01": {"events": list[EventEvidence], "history_clues": list[HistoryEvidence],
            "recommendation_only": Literal[True]},
    "X02": {"reservation_id": StrictStr, "amount_minor": StrictInt, "ledger_balance_unchanged": StrictBool},
    "X03": {"quote_id": StrictStr, "order_id": StrictStr, "item": StrictStr, "status": StrictStr},
    "X04": {"quote_id": StrictStr, "order_id": StrictStr, "item": StrictStr, "status": StrictStr},
    "X05": {"card_id": StrictStr, "freeze_status": StrictStr, "loss_report_status": StrictStr,
            "payment_blocked": StrictBool},
}


_REGISTRY: dict[str, tuple[str, str | None, str]] = {
    "Q01": ("account_exists", None, "balance_snapshot"),
    "Q02": ("transaction_owner", None, "transaction_ids"),
    "T01": ("recipient_owned,account_active,sufficient_funds", "transfer.reversal", "operation_and_ledger"),
    "T02": ("phone_resolves_to_owned_recipient", "transfer.reversal", "operation_and_ledger"),
    "T03": ("alias_resolves_to_owned_recipient", "transfer.reversal", "operation_and_ledger"),
    "T04": ("recipient_owned,mandate_active,account_active", "schedule.cancel", "mandate_and_job_ids"),
    "T05": ("participants_distinct", "bill.cancel", "bill_and_share_ids"),
    "B01": ("transaction_owner", None, "query_hash_and_transaction_ids"),
    "B02": ("transaction_owner", None, "reason_code_and_transaction_ids"),
    "B03": ("transaction_owner", None, "monthly_transaction_ids"),
    "B04": ("transaction_owner", None, "monthly_and_annual_transaction_ids"),
    "W01": ("risk_profile_current", None, "product_ids"),
    "W02": ("products_exist", None, "product_ids"),
    "W03": ("answers_complete", "risk_profile.replace", "risk_profile"),
    "W04": ("risk_suitable,account_funded", "investment.order.cancel", "order_operation_position_ids"),
    "W05": ("redeemable_order,position_available", "investment.redemption.cancel", "order_and_settlement_at"),
    "W06": ("positions_owned", None, "position_and_order_ids"),
    "C01": ("user_exists", "card.application.cancel", "card_and_application_ids"),
    "C02": ("card_owned,limit_policy", "card.limit.restore", "card_id_and_limit"),
    "C03": ("card_owned", "card.restriction.remove", "card_id_and_restriction"),
    "C04": ("card_owned", "card.restriction.restore", "card_id_and_restriction"),
    "C05": ("card_owned", "card.reissue", "card_id_and_status"),
    "C06": ("card_owned_and_lost", "card.freeze", "card_id_and_status"),
    "C07": ("card_owned", "card.freeze_or_restore", "card_id_and_payment_state"),
    "C08": ("card_owned,pin_policy", "pin.restore", "card_id_and_secret_persistence_flag"),
    "S01": ("subscription_owner", None, "subscription_ids"),
    "S02": ("subscription_owner", "reminder.cancel", "reminder_and_subscription_ids"),
    "S03": ("subscription_owner", "subscription.reactivate", "subscription_and_mandate_state"),
    "X01": ("event_owner", None, "event_ids_and_history_txn_ids"),
    "X02": ("account_active,sufficient_funds", "funds.release", "reservation_id"),
    "X03": ("delivery_date_valid,account_or_reservation_funded", "merchant.cancel_and_release", "order_operation_reservation_ids"),
    "X04": ("delivery_date_valid,account_or_reservation_funded", "merchant.cancel_and_release", "order_operation_reservation_ids"),
    "X05": ("card_owned_and_active", "card.unfreeze_if_report_aborted", "card_id_and_payment_state"),
}


class CapabilityContract:
    def __init__(self, capability_id: str) -> None:
        self.capability_id = capability_id
        self.input_model = INPUT_MODELS[capability_id]
        self.result_models = {
            "authorization": _result_model(f"{capability_id}Authorization", {
                **_AUTH_REQUIRED, "payload": (dict[str, Any], Field(default_factory=dict)),
            }),
            "business": _result_model(f"{capability_id}Result", _OUTPUT_FIELDS[capability_id]),
            "inactive": _result_model(f"{capability_id}Inactive", {
                "status": Literal["cancelled", "invalidated"],
            }),
        }
        if capability_id in {"T01", "T02", "T03"}:
            self.result_models["prepared"] = _result_model(f"{capability_id}Prepared", {
                **_AUTH_REQUIRED, "plan_id": StrictStr, "tier_display": StrictStr,
                "display": list[dict[str, StrictStr]],
            })
        if capability_id == "X05":
            self.result_models["partial"] = _result_model("X05PartialResult", {
                "card_id": StrictStr, "freeze_status": Literal["frozen"],
                "loss_report_status": Literal["awaiting_mfa"], "next_step": Literal["verify_loss_report"],
            })
        if capability_id == "T05":
            self.result_models["preview"] = _result_model("T05Preview", {
                "shares": dict[str, StrictInt], "preview_only": Literal[True],
            })
            self.result_models["paid"] = _result_model("T05Paid", {
                "bill_id": StrictStr, "share_status": StrictStr, "bill_status": StrictStr,
                "amount_minor": (StrictInt, None), "operation_id": (StrictStr, None),
            })
            self.result_models["bill_status"] = _result_model("T05BillStatus", {
                "bill_id": StrictStr, "status": StrictStr, "shares": list[BillShareEvidence],
            })
        if capability_id == "W05":
            self.result_models["settled"] = _result_model("W05Settled", {
                "order_id": StrictStr, "status": Literal["settled"], "operation_id": StrictStr,
            })
        if capability_id in {"T04", "X03", "X04"}:
            self.result_models["paused"] = _result_model(f"{capability_id}Paused", {
                "error_code": StrictStr,
                **({"schedule_id": StrictStr, "mandate_id": StrictStr} if capability_id == "T04"
                   else {"order_id": StrictStr, "status": StrictStr}),
            })
        if capability_id in {"X03", "X04"}:
            self.result_models["failed"] = _result_model(f"{capability_id}Failed", {
                "order_id": StrictStr, "status": Literal["failed"],
            })
        if capability_id == "X02":
            self.result_models["released"] = _result_model("X02Released", {
                "reservation_id": StrictStr, "status": Literal["released"],
            })
        preconditions, compensation, evidence = _REGISTRY[capability_id]
        self.preconditions = preconditions.split(",")
        self.compensation = compensation
        self.evidence = evidence.split(",")
        self.card = {"title": capability_id,
                     "fields": [field for field in _OUTPUT_FIELDS[capability_id] if field not in {"new_pin", "otp", "device_token"}],
                     "show_evidence": True}

    def json_schema(self) -> dict[str, Any]:
        return {"input": input_json_schema(self.capability_id),
                "authorization_result": self.result_models["authorization"].model_json_schema(),
                "business_result": self.result_models["business"].model_json_schema(),
                "preconditions": list(self.preconditions), "compensation": self.compensation,
                "evidence": list(self.evidence), "card": dict(self.card)}


CAPABILITY_CONTRACTS = {capability_id: CapabilityContract(capability_id) for capability_id in INPUT_MODELS}


def validate_input(capability_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if capability_id not in INPUT_MODELS:
        raise ValueError("CAPABILITY_NOT_REGISTERED")
    if not isinstance(payload, dict):
        raise TypeError("capability payload must be an object")
    compatibility = {key: payload[key] for key in ("device_token", "otp")
                     if key in payload and capability_id in _OTP_CAPABILITIES}
    parsed = INPUT_MODELS[capability_id].model_validate({key: value for key, value in payload.items()
                                                         if key not in compatibility})
    for key, value in compatibility.items():
        if not isinstance(value, str):
            raise TypeError(f"{key} must be a string")
    values = parsed.model_dump(exclude_unset=True, exclude_none=True)
    values.update(compatibility)
    return values


def validate_result(capability_id: str, status: str, data: dict[str, Any]) -> dict[str, Any]:
    contract = CAPABILITY_CONTRACTS.get(capability_id)
    if contract is None:
        raise ValueError("CAPABILITY_NOT_REGISTERED")
    if status not in {"succeeded", "created", "failed", "paused", "cancelled", "invalidated",
                      "awaiting_confirmation", "awaiting_mfa", "prepared", "scheduled", "processing",
                      "pending", "reserved", "released", "partially_completed", "settled",
                      "cooling_off", "awaiting_reconfirmation", "handoff"}:
        raise ValueError("CAPABILITY_RESULT_STATUS")
    model_name = "authorization" if status in {"awaiting_confirmation", "awaiting_mfa", "prepared"} else "business"
    if status == "prepared" and "prepared" in contract.result_models:
        model_name = "prepared"
    if capability_id == "T05" and data.get("preview_only"):
        model_name = "preview"
    elif capability_id == "T05" and data.get("share_status"):
        model_name = "paid"
    elif capability_id == "T05" and isinstance(data.get("shares"), list):
        model_name = "bill_status"
    if status == "paused" and "paused" in contract.result_models:
        model_name = "paused"
    if status == "failed" and "failed" in contract.result_models:
        model_name = "failed"
    if status == "settled" and "settled" in contract.result_models:
        model_name = "settled"
    if status == "partially_completed" and "partial" in contract.result_models:
        model_name = "partial"
    if status in {"cancelled", "invalidated"}:
        model_name = "inactive"
    if capability_id == "X02" and data.get("status") == "released":
        model_name = "released"
    return contract.result_models[model_name].model_validate(data).model_dump(exclude_unset=True)


def input_json_schema(capability_id: str) -> dict[str, Any]:
    if capability_id not in INPUT_MODELS:
        raise ValueError("CAPABILITY_NOT_REGISTERED")
    schema = INPUT_MODELS[capability_id].model_json_schema()
    schema["properties"] = {key: value for key, value in schema.get("properties", {}).items()
                            if key not in {"device_token", "otp", "new_pin"}}
    return schema


def _validate_ranges(capability_id: str, payload: dict[str, Any]) -> None:
    for key in ("amount_minor", "total_minor", "limit_minor", "max_uses", "limit"):
        if key in payload and payload[key] is not None and payload[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if "days" in payload and payload["days"] < 0:
        raise ValueError("days must be nonnegative")
    if "limit" in payload and payload["limit"] > 50_000:
        raise ValueError("limit exceeds maximum")
    if capability_id == "W03" and "answers" in payload:
        answers = payload["answers"]
        if len(answers) != 10 or any(answer < 0 or answer > 5 for answer in answers):
            raise ValueError("answers must contain ten values in [0, 5]")
    if capability_id == "T05":
        action = payload.get("action")
        required = {"preview": {"total_minor", "participant_ids"},
                    "status": {"bill_id"}, "pay_share": {"bill_id"}}
        if action in required and not required[action] <= payload.keys():
            raise ValueError(f"{action} requires {sorted(required[action])}")
        participants = payload.get("participant_ids", [])
        if len(participants) != len(set(participants)):
            raise ValueError("participant_ids must be unique")
    if capability_id == "B04" and "year" in payload and not (1 <= int(payload["year"]) <= 9999):
        raise ValueError("year out of range")
