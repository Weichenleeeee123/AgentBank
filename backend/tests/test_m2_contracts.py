import pytest
from pydantic import ValidationError

from agentbank.contracts import AnalysisDslRequest, AASplitRequest, RiskQuestionnaireRequest


def test_analysis_contract_limits_rows_and_rejects_other_sources() -> None:
    request = AnalysisDslRequest(group_by=["month"], limit=100)
    assert request.source == "transactions"
    with pytest.raises(ValidationError):
        AnalysisDslRequest(source="users")
    with pytest.raises(ValidationError):
        AnalysisDslRequest(limit=50_001)


def test_risk_and_aa_contracts_use_strict_minor_units() -> None:
    assert len(RiskQuestionnaireRequest(answers=[1] * 10).answers) == 10
    assert AASplitRequest(total_minor=10_000, participant_ids=["u1", "u2"]).total_minor == 10_000
    with pytest.raises(ValidationError):
        AASplitRequest(total_minor=10.5, participant_ids=["u1", "u2"])
