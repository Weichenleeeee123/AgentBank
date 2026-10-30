import pytest
from pydantic import ValidationError

from agentbank.capability_contracts import (
    CAPABILITY_CONTRACTS,
    input_json_schema,
    validate_input,
    validate_result,
)
from agentbank.tools import CAPABILITIES


def test_every_capability_has_input_output_and_registry_metadata() -> None:
    assert set(CAPABILITY_CONTRACTS) == set(CAPABILITIES)
    assert all(contract.input_model.model_json_schema() for contract in CAPABILITY_CONTRACTS.values())
    assert all(contract.result_models for contract in CAPABILITY_CONTRACTS.values())
    assert all(contract.preconditions and contract.evidence for contract in CAPABILITY_CONTRACTS.values())


def test_inputs_forbid_unknown_fields_and_strictly_validate_ranges_and_dates() -> None:
    with pytest.raises(ValidationError):
        validate_input("Q01", {"user_id": "other"})
    with pytest.raises(ValidationError):
        validate_input("X02", {"amount_minor": True})
    with pytest.raises(ValueError):
        validate_input("X02", {"amount_minor": -1})
    with pytest.raises(ValidationError):
        validate_input("X03", {"delivery_date": "2026-02-30"})
    with pytest.raises(ValidationError):
        validate_input("W03", {"answers": [True] * 10})


def test_sensitive_otp_fields_remain_accepted_but_absent_from_generated_schema() -> None:
    validate_input("W04", {"assurance": "mfa", "device_token": "opaque", "otp": "123456"})
    schema = input_json_schema("W04")
    assert "device_token" not in str(schema)
    assert "otp" not in str(schema)


@pytest.mark.parametrize("capability_id", sorted(CAPABILITIES))
def test_business_result_requires_capability_specific_evidence(capability_id: str) -> None:
    with pytest.raises(ValidationError):
        validate_result(capability_id, "succeeded", {})


def test_result_contract_distinguishes_authorization_from_terminal_result() -> None:
    prepared = validate_result("S03", "awaiting_confirmation", {
        "receipt_id": "receipt", "action_hash": "sha256:action", "display_hash": "sha256:display",
        "assurance": "confirm",
    })
    assert prepared["receipt_id"] == "receipt"
    with pytest.raises(ValidationError):
        validate_result("S03", "succeeded", {"receipt_id": "receipt"})
    result = validate_result("S03", "succeeded", {
        "subscription_id": "sub", "status": "cancelled", "debit_mandate": "revoked",
    })
    assert result["debit_mandate"] == "revoked"


def test_input_schema_has_no_sensitive_fields_even_when_compatibly_accepted() -> None:
    for capability_id in ("W04", "W05", "C01", "C02", "C04", "C05", "C06", "C08", "X05"):
        schema = input_json_schema(capability_id)
        assert "device_token" not in str(schema)
        assert '"otp"' not in str(schema)


def test_real_cap_journeys_have_valid_status_specific_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentbank.bank.capabilities_full import CapabilityService
    from agentbank.evals.runner import _run_capability_detailed

    original = CapabilityService._result
    errors: list[str] = []
    observed: set[str] = set()

    def checked_result(self, record):
        result = original(self, record)
        observed.add(result.capability_id)
        try:
            validate_result(result.capability_id, result.status, result.data)
        except (ValidationError, ValueError) as error:
            errors.append(f"{result.capability_id}/{result.status}: {error}")
        return result

    monkeypatch.setattr(CapabilityService, "_result", checked_result)
    for capability_id in sorted(CAPABILITIES):
        for variant in (1, 2, 3):
            _run_capability_detailed({"capability_id": capability_id, "variant": variant})
    assert not errors, "\n".join(errors)
    assert observed == set(CAPABILITIES)
