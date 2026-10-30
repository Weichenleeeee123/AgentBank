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


def test_capability_catalogue_has_model_safe_slot_contracts() -> None:
    catalogue = LLMGateway.capability_catalogue()
    assert catalogue["W02"]["required_slots"] == []
    assert catalogue["B04"]["slots"]["year"]["type"] == "string"
    assert "assurance" not in catalogue["W04"]["slots"]
    assert "device_token" not in catalogue["W04"]["slots"]
    assert "otp" not in catalogue["W04"]["slots"]
    assert "new_pin" not in catalogue["C08"]["slots"]
    assert "simulate_failure" not in catalogue["X03"]["slots"]
    assert catalogue["W02"]["name"] == "比较指定理财产品"


def test_natural_language_action_is_rejected_and_retried(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'runtime-action.db'}")
    load_seed(database)
    requests: list[dict[str, object]] = []

    def transport(config: ProviderConfig, role: str, payload: dict[str, object]) -> dict[str, object]:
        requests.append(payload)
        if len(requests) == 1:
            return {"intent": "balance", "capability_id": "Q01", "slots": {}, "missing": [],
                    "ambiguities": [], "suggested_next_action": "执行"}
        return {"intent": "balance", "capability_id": "Q01", "slots": {}, "missing": [],
                "ambiguities": [], "suggested_next_action": "execute"}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"), None,
                         transport=transport)
    result = gateway.plan_intent("查余额", user_id="user_001")
    assert result["suggested_next_action"] == "execute"
    assert len(requests) == 2
    schema = requests[0]["response_schema"]
    assert isinstance(schema, dict)
    assert schema["suggested_next_action"] == ("clarify", "explore", "execute", "confirm")


def test_read_only_catalogue_excludes_subscription_list() -> None:
    assert "S01" not in LLMGateway.READ_ONLY_CAPABILITIES


def test_critical_slots_require_grounding_from_text_or_structured_observation(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'runtime-provenance.db'}")
    load_seed(database)

    def transport(config: ProviderConfig, role: str, payload: dict[str, object]) -> dict[str, object]:
        return {"intent": "transfer", "capability_id": "T01", "slots": {
            "recipient_name": "张三", "amount_minor": 5}, "missing": [], "ambiguities": [],
            "suggested_next_action": "execute"}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"), None,
                         transport=transport)
    from_text = gateway.plan_intent("给张三转500元", user_id="user_001")
    assert from_text["slots"]["recipient_name"] == "张三"
    assert "amount_minor" not in from_text["slots"]
    assert "amount_minor" in from_text["missing"]

    from_observation = gateway.plan_intent("给他转账", user_id="user_001", context={
        "exploration": [{"observation": {"recipient_name": "张三", "amount_minor": 5}}]})
    assert from_observation["slots"] == {"recipient_name": "张三", "amount_minor": 5}


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
