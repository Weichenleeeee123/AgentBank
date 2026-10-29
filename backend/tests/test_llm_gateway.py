from pathlib import Path

from agentbank.db import AuditLog, Database
from agentbank.llm.gateway import LLMGateway, ProviderConfig
from agentbank.seed.loader import load_seed


def test_gateway_uses_primary_and_audits_only_hashes(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'llm.db'}")
    load_seed(database)
    calls: list[str] = []

    def transport(config: ProviderConfig, role: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append(config.name)
        return {"intent": "balance", "confidence": 0.9}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"),
                         ProviderConfig("backup", "https://backup.invalid", "secret2", "model-b"), transport=transport)
    result = gateway.classify_intent("查余额", user_id="user_001")
    assert result["intent"] == "balance"
    assert calls == ["primary"]
    with database.sessions() as session:
        event = session.query(AuditLog).filter(AuditLog.event_type == "llm.call.completed").one()
        assert "查余额" not in event.payload_json
        assert "model-a" in event.payload_json


def test_gateway_falls_back_after_primary_failure(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'fallback.db'}")
    load_seed(database)
    calls: list[str] = []

    def transport(config: ProviderConfig, role: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append(config.name)
        if config.name == "primary":
            raise TimeoutError("primary timeout")
        return {"intent": "unknown", "confidence": 0.2}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"),
                         ProviderConfig("backup", "https://backup.invalid", "secret2", "model-b"), transport=transport)
    result = gateway.classify_intent("闲聊", user_id="user_001")
    assert result["intent"] == "unknown"
    assert calls == ["primary", "primary", "backup"]
