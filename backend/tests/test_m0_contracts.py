import pytest
from pydantic import ValidationError

from agentbank.contracts import (
    Assurance,
    Decision,
    IntentFrame,
    OperationStatus,
    ToolDefinition,
    ToolKind,
    ToolResult,
    TransferCreateRequest,
)


def test_transfer_contract_uses_minor_units_and_server_owned_identity() -> None:
    request = TransferCreateRequest(
        source_account_id="acc_001",
        recipient_id="payee_001",
        amount_minor=20_000,
        currency="CNY",
        memo="房租",
    )

    assert request.amount_minor == 20_000
    assert request.currency == "CNY"
    assert "user_id" not in TransferCreateRequest.model_fields


def test_transfer_contract_rejects_float_amount_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TransferCreateRequest(
            source_account_id="acc_001",
            recipient_id="payee_001",
            amount_minor=200.5,
            currency="CNY",
        )

    with pytest.raises(ValidationError):
        TransferCreateRequest(
            source_account_id="acc_001",
            recipient_id="payee_001",
            amount_minor=20_000,
            currency="CNY",
            user_id="attacker",
        )


def test_tool_definition_exposes_input_schema_but_keeps_security_separate() -> None:
    definition = ToolDefinition(
        name="transfer.create",
        version="1.0",
        kind=ToolKind.command,
        input_schema={"type": "object", "additionalProperties": False},
        security={"minimum_assurance": Assurance.confirm, "resource_owner_check": True},
        preconditions=["source_account.status == active"],
        execution={"idempotent": True, "timeout_ms": 5_000},
        evidence_fields=["operation_id", "ledger_entry_ids", "status"],
    )

    assert definition.model_dump(mode="json")["security"]["minimum_assurance"] == "confirm"
    assert definition.model_dump(mode="json")["execution"]["idempotent"] is True


def test_result_envelope_distinguishes_unknown_from_processing() -> None:
    for status in (OperationStatus.unknown, OperationStatus.processing):
        result = ToolResult(
            operation_id="op_001",
            status=status,
            data={},
            evidence={"source": "simulated_bank"},
        )
        assert result.status is status


def test_intent_frame_keeps_unresolved_amount_as_missing() -> None:
    frame = IntentFrame(
        intent="transfer",
        slots={"recipient_hint": "房东", "amount_text": None},
        references=[],
        missing_required=["amount"],
        ambiguities=[],
        suggested_next_action="ask_user",
    )

    assert frame.missing_required == ["amount"]


def test_decision_serializes_policy_version_and_assurance() -> None:
    decision = Decision(
        decision_id="dec_001",
        policy_version="2026-10-28.1",
        operation_id="op_001",
        action_hash="sha256:abc",
        executability="allow",
        assurance=Assurance.confirm,
        channel_requirement="any",
        tier_display="Y",
        reason_codes=["P05"],
        facts={"candidate_minor": 20_000},
        warnings=[],
    )

    assert decision.model_dump(mode="json")["assurance"] == "confirm"
    assert decision.tier_display == "Y"
