from agentbank.tools import TOOL_REGISTRY


def test_registry_security_levels_match_plan() -> None:
    assert TOOL_REGISTRY["W04"]["security"]["minimum_assurance"] == "mfa"
    assert TOOL_REGISTRY["W05"]["security"]["minimum_assurance"] == "mfa"
    assert TOOL_REGISTRY["W01"]["security"]["minimum_assurance"] == "none"
    assert TOOL_REGISTRY["C07"]["security"]["protective"] is True
    assert TOOL_REGISTRY["C06"]["security"]["protective"] is False
