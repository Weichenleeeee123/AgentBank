from pathlib import Path

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.db import AuditLog, CapabilityRecord, Database, HandoffCase
from agentbank.llm.gateway import LLMGateway, ProviderConfig
from agentbank.seed.loader import load_seed


def test_fraud_role_is_schema_validated_audited_and_independent(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'fraud-role.db'}")
    load_seed(database)
    requests = []

    def transport(config, role, payload):
        requests.append((role, payload))
        assert role == "fraud"
        return {"category": "impersonation", "confidence": 0.98}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://provider.invalid", "secret", "m"), None,
                         transport=transport)
    result = gateway.classify_fraud("对方要求我付款以证明清白", user_id="user_001", recent_user_messages=["有人冒充公安"])
    assert result["category"] == "impersonation"
    assert requests[0][1]["temperature"] == 0
    assert requests[0][1]["prompt_version"] == "agent-fraud-v1"
    with database.sessions() as session:
        event = session.query(AuditLog).filter(AuditLog.event_type == "llm.call.completed").one()
        assert '"role": "fraud"' in event.payload_json
        assert "证明清白" not in event.payload_json


def test_fraud_model_high_cannot_reach_transfer_prepare(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'fraud-block.db'}")
    load_seed(database)
    calls = []

    def transport(config, role, payload):
        calls.append(role)
        if role == "planner":
            return {"intent": "transfer", "capability_id": "T01",
                    "slots": {"recipient_name": "房东", "amount_minor": 10000},
                    "missing": [], "ambiguities": [], "suggested_next_action": "confirm"}
        return {"category": "impersonation", "confidence": 0.99}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://provider.invalid", "secret", "m"), None,
                         transport=transport)
    response = AgentOrchestrator(database, llm=gateway).handle("user_001", "fraud-chat", "给房东转100元")
    assert response.kind == "error"
    assert response.data["reason_code"] == "FRAUD_REVIEW_REQUIRED"
    assert calls == ["planner", "fraud"]
    with database.sessions() as session:
        assert session.query(CapabilityRecord).count() == 0
        assert session.query(HandoffCase).count() == 1


def test_invalid_fraud_verdict_retries_then_returns_unknown(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'fraud-invalid.db'}")
    load_seed(database)
    calls = []

    def transport(config, role, payload):
        calls.append(config.name)
        return {"category": "safe", "confidence": True}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://provider.invalid", "secret", "m"),
                         ProviderConfig("backup", "https://backup.invalid", "secret", "m"), transport=transport)
    assert gateway.classify_fraud("给房东付款", user_id="user_001")["category"] == "unknown"
    assert calls == ["primary", "primary", "backup"]
