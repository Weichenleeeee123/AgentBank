from pathlib import Path

import pytest

from agentbank.evals.runner import CAPABILITY_IDS, _run_capability_detailed, run_cases


def test_all_capabilities_have_three_independent_journeys_with_evidence() -> None:
    report = run_cases(Path(__file__).resolve().parents[2] / "evals")
    capability_cases = [item for item in report.cases if item.kind == "capability"]

    assert len(capability_cases) == len(CAPABILITY_IDS) * 3
    assert all(item.outcome == "passed" for item in capability_cases)
    assert {item.evidence["branch"] for item in capability_cases} >= {
        "strict_prepare_confirm_otp", "deterministic_agent_multiround", "boundary_recovery",
        "secure_form_direct_C08",
    }
    assert all(item.evidence.get("terminal_state") for item in capability_cases)
    assert all(item.evidence.get("audit_event") or item.evidence["variant"] == 3 for item in capability_cases)


def test_runner_rejects_unknown_case_kinds(tmp_path: Path) -> None:
    (tmp_path / "unknown.yaml").write_text("case_id: BAD\nkind: invented\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown evaluation case kind"):
        run_cases(tmp_path)


@pytest.mark.parametrize("capability_id,variant", [("T01", 2), ("T04", 2), ("W06", 1),
                                                   ("W06", 2), ("W05", 3), ("C08", 3),
                                                   ("X03", 3), ("X04", 3)])
def test_cap_journey_integration_terminal_states(capability_id: str, variant: int) -> None:
    passed, evidence, errors = _run_capability_detailed({"capability_id": capability_id,
                                                       "variant": variant})
    assert passed, errors or evidence
    state = evidence["terminal_state"]
    if capability_id in {"X03", "X04"}:
        assert state["reservation_status"] == "released"
        assert state["available_minor"] == state["initial_available_minor"]
        assert state["ledger_entry_count"] == 0
    elif capability_id == "W05":
        assert state["error"] == "ORDER_NOT_REDEEMABLE"
        assert state["new_orders"] == 0
    elif capability_id == "C08":
        assert state["chat_rejected_pin"] is True
        assert state["persisted_pin_occurrences"] == 0
