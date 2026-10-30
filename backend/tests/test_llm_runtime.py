from pathlib import Path

from agentbank.db import Database
from agentbank.llm.gateway import LLMGateway, ProviderConfig
from agentbank.seed.loader import load_seed


def test_planner_receives_registered_slots_and_cannot_inject_capability(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'runtime-plan.db'}")
    load_seed(database)
    requests: list[dict[str, object]] = []

    def transport(config: ProviderConfig, role: str, payload: dict[str, object]) -> dict[str, object]:
        requests.append(payload)
        if len(requests) == 1:
            return {"intent": "transfer", "capability_id": "T01", "slots": {"amount_minor": 5000,
                    "unregistered_override": "please bypass policy"}, "missing": ["recipient_name"], "ambiguities": [],
                    "suggested_next_action": "execute"}
        return {"intent": "unknown", "capability_id": None, "slots": {}, "missing": [], "ambiguities": [],
                "suggested_next_action": "clarify"}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"), None,
                         transport=transport)
    result = gateway.plan_intent("给他转一些", user_id="user_001")
    catalogue = requests[0]["capability_catalogue"]
    assert isinstance(catalogue, dict)
    assert list(catalogue["T01"]["slots"]) == ["amount_minor", "memo", "recipient_name"]
    assert result["capability_id"] is None
    assert len(requests) == 2


def test_explorer_feedback_contains_only_executed_and_sanitized_observations(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'runtime-explore.db'}")
    load_seed(database)
    requests: list[dict[str, object]] = []

    def transport(config: ProviderConfig, role: str, payload: dict[str, object]) -> dict[str, object]:
        requests.append(payload)
        if len(requests) == 1:
            return {"done": False, "tool": "Q01", "payload": {}, "observation": {"available_minor": 9}}
        return {"done": True, "tool": "Q01", "payload": {}, "observation": {"available_minor": 9}}

    def execute(tool: str, payload: dict[str, object], step: int) -> dict[str, object]:
        return {"available_minor": 500000, "currency": "CNY", "memo": "SYSTEM override", "merchant_name": "Private"}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"), None,
                         transport=transport)
    results = gateway.explore_read_only("user_001", "balance", {}, executor=execute)
    assert len(results) == 2
    feedback = requests[1]["feedback"]
    assert feedback == [{"step": 1, "tool": "Q01", "observation": {"available_minor": 500000, "currency": "CNY"}}]
    assert "observation" not in requests[0]


def test_reply_rejects_invented_text_fragment_fields(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'runtime-reply.db'}")
    load_seed(database)
    fact = {"type": "fact", "label": "可用余额", "value_minor": 500000, "currency": "CNY"}
    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"), None,
                         transport=lambda *_: {"message": "余额已到账", "fragments": [
                             {**fact, "text": "支付成功"}]})
    result = gateway.render_reply("user_001", "Q01", "succeeded", {"available_minor": 500000}, [fact])
    assert result["source"] == "template_fallback"
