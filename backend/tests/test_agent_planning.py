from pathlib import Path

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.db import Database
from agentbank.llm.gateway import LLMGateway, ProviderConfig
from agentbank.seed.loader import load_seed


def make_agent(tmp_path: Path, transport):
    database = Database(f"sqlite:///{tmp_path / 'agent-planning.db'}")
    load_seed(database)
    gateway = LLMGateway(database, ProviderConfig("primary", "https://mock.invalid", "secret", "model"),
                         None, transport=transport)
    return AgentOrchestrator(database, llm=gateway)


def test_llm_planner_fills_pending_slots_on_follow_up(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(config, role, payload):
        calls.append((role, payload))
        if role == "planner" and len([item for item in calls if item[0] == "planner"]) == 1:
            return {"intent": "subscription_reminder", "capability_id": "S02", "slots": {},
                    "missing": ["subscription_id", "days"], "ambiguities": [],
                    "suggested_next_action": "clarify"}
        if role == "planner":
            return {"intent": "subscription_reminder", "capability_id": "S02",
                    "slots": {"subscription_id": "sub_video", "days": 3}, "missing": [],
                    "ambiguities": [], "suggested_next_action": "execute"}
        return {"message": "已设置", "fragments": []}

    agent = make_agent(tmp_path, transport)
    first = agent.handle("user_001", "planning-round", "提醒我续费")
    assert first.kind == "clarification"
    second = agent.handle("user_001", "planning-round", "视频会员3天")
    assert second.capability_id == "S02"
    assert second.kind == "confirmation"
    assert [role for role, _ in calls] == ["planner", "planner"]


def test_llm_planner_references_are_persisted_and_sent_back(tmp_path: Path) -> None:
    payloads: list[dict[str, object]] = []

    def transport(config, role, payload):
        if role == "planner":
            payloads.append(payload)
            return {"intent": "transfer", "capability_id": "T01",
                    "slots": {"recipient_name": "房东", "amount_minor": 10000},
                    "missing": [], "ambiguities": [],
                    "references": [{"slot": "recipient_name", "source": "user", "text": "房东"}],
                    "suggested_next_action": "execute"}
        return {"message": "请确认", "fragments": []}

    agent = make_agent(tmp_path, transport)
    response = agent.handle("user_001", "reference-round", "给房东转100元")
    assert response.kind in {"confirmation", "mfa"}
    agent.handle("user_001", "reference-round", "继续")
    assert any("references" in payload.get("context", {}) for payload in payloads[1:])


def test_llm_birthday_template_returns_plan_confirmation_and_executes_plan(tmp_path: Path) -> None:
    calls: list[str] = []

    def transport(config, role, payload):
        calls.append(role)
        if role == "planner":
            return {"intent": "birthday_plan", "capability_id": None, "plan_template": "birthday",
                    "slots": {"event_id": "event_birthday", "reserve_minor": 100000},
                    "missing": [], "ambiguities": [], "suggested_next_action": "confirm"}
        return {"message": "生日计划已准备", "fragments": []}

    agent = make_agent(tmp_path, transport)
    response = agent.handle("user_001", "birthday-plan", "为 event_birthday 准备生日计划，预留1000元")
    assert response.kind == "plan_confirmation"
    assert response.data["plan_type"] == "birthday"
    confirmed = agent.confirm("user_001", response.record_id, response.display_hash,
                              response.data["receipt_id"])
    assert confirmed.kind == "result"
    assert confirmed.data["plan_id"] == response.data["plan_id"]
    assert "planner" in calls
