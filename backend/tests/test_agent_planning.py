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


def test_llm_outage_uses_deterministic_fallback_and_does_not_execute_write(tmp_path: Path) -> None:
    def transport(config, role, payload):
        if role == "fraud":
            return {"category": "safe", "confidence": 0.99}
        raise TimeoutError("offline provider")

    agent = make_agent(tmp_path, transport)
    response = agent.handle("user_001", "planner-outage", "给房东转100元")
    assert response.kind == "confirmation"
    assert response.data["payload"]["amount_minor"] == 10000


def test_model_dag_defaults_server_generated_node_metadata(tmp_path: Path) -> None:
    def fraud_transport(config, role, payload):
        if role == "fraud":
            return {"category": "safe", "confidence": 0.99}
        if role == "planner":
            return {"intent": "check balance", "capability_id": None, "plan_template": None,
                    "slots": {}, "missing": [], "ambiguities": [], "references": [],
                    "suggested_next_action": "confirm",
                    "proposed_dag": [{"id": "balance", "tool": "Q01", "args": {}, "depends_on": []}]}
        return {"message": "余额已确认", "fragments": []}

    agent = make_agent(tmp_path, fraud_transport)
    response = agent.handle("user_001", "model-dag-defaults", "查余额并生成计划")

    assert response.kind == "plan_confirmation"
    assert response.data["nodes"][0]["idempotency_key"].startswith(response.data["plan_id"])


def test_model_dag_confirmation_routes_mfa_plans_to_verification(tmp_path: Path) -> None:
    def transport(config, role, payload):
        if role == "planner":
            return {"intent": "investment", "capability_id": None, "plan_template": None,
                    "slots": {}, "missing": [], "ambiguities": [], "references": [],
                    "suggested_next_action": "confirm",
                    "proposed_dag": [{"id": "subscribe", "tool": "W04",
                                      "args": {"product_id": "product_R1", "amount_minor": 10_000},
                                      "depends_on": []}]}
        return {"message": "已完成", "fragments": []}

    agent = make_agent(tmp_path, transport)
    from agentbank.bank.capabilities_full import CapabilityService
    CapabilityService(agent.database).execute("user_001", "W03", {"answers": [3] * 10}, "agent-dag-risk")
    prepared = agent.handle("user_001", "model-dag-mfa", "申购 R1 理财 100 元")

    assert prepared.kind == "plan_confirmation"
    confirmed = agent.confirm("user_001", prepared.record_id or "", prepared.display_hash,
                              str(prepared.data["receipt_id"]))
    assert confirmed.kind == "mfa"
    assert confirmed.data["status"] == "awaiting_mfa"


def test_deterministic_amount_normalization_overrides_model_amount(tmp_path: Path) -> None:
    def transport(config, role, payload):
        if role == "fraud":
            return {"category": "safe", "confidence": 0.99}
        return {"intent": "transfer", "capability_id": "T01", "slots": {
                    "recipient_name": "房东", "amount_minor": 100}, "missing": [],
                "ambiguities": [], "suggested_next_action": "execute"}

    agent = make_agent(tmp_path, transport)
    response = agent.handle("user_001", "planner-amount", "给房东转100元")
    assert response.kind == "confirmation"
    assert response.data["payload"]["amount_minor"] == 10000


def test_transfer_family_uses_normalized_payee_resolution_mode(tmp_path: Path) -> None:
    def transport(config, role, payload):
        if role == "fraud":
            return {"category": "safe", "confidence": 0.99}
        return {"intent": "transfer", "capability_id": "T03",
                "slots": {"alias": "房东", "amount_minor": 10000}, "missing": [],
                "ambiguities": ["模型提出但确定性收款人解析已消歧"], "suggested_next_action": "execute"}

    agent = make_agent(tmp_path, transport)
    response = agent.handle("user_001", "planner-family", "给房东转100元")
    assert response.kind == "confirmation"
    assert response.capability_id == "T01"
    assert response.data["payload"]["recipient_name"] == "房东"
    assert response.data["payload"]["amount_minor"] == 10000


def test_llm_planner_references_are_persisted_and_sent_back(tmp_path: Path) -> None:
    payloads: list[dict[str, object]] = []

    def transport(config, role, payload):
        if role == "fraud":
            return {"category": "safe", "confidence": 0.99}
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


def test_llm_composite_plan_uses_registered_read_only_template_and_executes(tmp_path: Path) -> None:
    calls: list[str] = []

    def transport(config, role, payload):
        calls.append(role)
        if role == "planner":
            return {"intent": "monthly_report", "capability_id": None,
                    "plan_template": "query_analysis_report",
                    "slots": {"start_month": "2026-01", "end_month": "2026-03",
                              "group_by": ["category"], "report": "monthly"},
                    "missing": [], "ambiguities": [], "suggested_next_action": "confirm"}
        return {"message": "只读分析计划已准备", "fragments": []}

    agent = make_agent(tmp_path, transport)
    response = agent.handle("user_001", "composite-report", "生成2026-01到2026-03的月度类别报告")
    assert response.kind == "plan_confirmation"
    assert response.data["plan_type"] == "query_analysis_report"
    result = agent.confirm("user_001", response.record_id, response.display_hash,
                           response.data["receipt_id"])
    assert result.kind == "result"
    assert result.data["status"] == "succeeded"
    assert [node["status"] for node in result.data["nodes"]] == ["succeeded"] * 3
    assert calls.count("planner") == 1
