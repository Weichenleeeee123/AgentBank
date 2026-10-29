from pathlib import Path

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.agent.intent import IntentParser
from agentbank.db import Database
from agentbank.llm.gateway import LLMGateway, ProviderConfig
from agentbank.seed.loader import load_seed


def _agent(tmp_path: Path) -> AgentOrchestrator:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    load_seed(database)
    return AgentOrchestrator(database)


def test_agent_handles_balance_as_fact_fragments(tmp_path: Path) -> None:
    response = _agent(tmp_path).handle("user_001", "conv-1", "帮我查一下余额")
    assert response.kind == "facts"
    assert response.intent == "balance"
    assert response.fragments[0]["type"] == "fact"
    assert response.fragments[0]["value_minor"] == 500_000


def test_agent_prepares_transfer_then_confirms_same_record(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    response = agent.handle("user_001", "conv-1", "给房东转 20 元，备注房租")
    assert response.kind == "confirmation"
    assert response.capability_id == "T03"
    assert response.record_id
    assert response.fragments[0]["type"] == "fact"
    confirmed = agent.confirm("user_001", response.record_id, response.display_hash)
    assert confirmed.kind == "result"
    assert confirmed.data["transfer_result"]["status"] == "succeeded"


def test_agent_asks_for_disambiguation_and_missing_amount(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    ambiguous = agent.handle("user_001", "conv-1", "给张三转钱")
    assert ambiguous.kind == "clarification"
    assert "收款人" in ambiguous.message
    missing = agent.handle("user_001", "conv-2", "给房东转账")
    assert missing.kind == "clarification"
    assert "金额" in missing.message


def test_agent_routes_red_transfer_to_mfa_without_executing(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    response = agent.handle("user_001", "conv-1", "给房东转 1200 元")
    assert response.kind == "mfa"
    assert response.capability_id == "T03"
    assert response.data["assurance"] == "mfa"


def test_agent_discovers_birthday_without_spending(tmp_path: Path) -> None:
    response = _agent(tmp_path).handle("user_001", "conv-1", "我爱人生日快到了，帮我看看")
    assert response.kind == "facts"
    assert response.capability_id == "X01"
    assert response.data["recommendation_only"] is True


def test_agent_continues_missing_slots_and_risk_questionnaire(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    first = agent.handle("user_001", "conv-slots", "给房东转账")
    assert first.kind == "clarification"
    second = agent.handle("user_001", "conv-slots", "20元")
    assert second.kind == "confirmation"
    risk = agent.handle("user_001", "conv-risk", "做风险测评")
    assert risk.kind == "clarification"
    complete = agent.handle("user_001", "conv-risk", "1 1 1 1 1 1 1 1 1 1")
    assert complete.kind == "confirmation"
    confirmed = agent.confirm("user_001", complete.record_id, complete.display_hash, complete.data["receipt_id"])
    assert confirmed.data["level"] == "C1"


def test_agent_routes_subscription_and_card_commands(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    subscriptions = agent.handle("user_001", "conv-sub", "帮我查一下订阅")
    assert subscriptions.capability_id == "S01"
    cancelled = agent.handle("user_001", "conv-sub-cancel", "取消视频会员")
    assert cancelled.capability_id == "S03"
    frozen = agent.handle("user_001", "conv-card", "先把我的卡冻结")
    assert frozen.capability_id == "C07"


def test_pending_slots_survive_agent_recreation_without_assistant_text(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent-state.db'}")
    load_seed(database)
    first_agent = AgentOrchestrator(database)
    first = first_agent.handle("user_001", "restart-conv", "给房东转账")
    assert first.kind == "clarification"
    restarted_agent = AgentOrchestrator(database)
    completed = restarted_agent.handle("user_001", "restart-conv", "20元")
    assert completed.kind == "confirmation"
    from agentbank.db import ConversationState
    with database.sessions() as session:
        state = session.get(ConversationState, ("user_001", "restart-conv"))
        assert state is not None
        assert state.pending_intent_json is None
        assert "assistant" not in state.recent_user_messages_json


def test_intent_parser_routes_the_remaining_m2_capabilities() -> None:
    parser = IntentParser()
    cases = {
        "每月给房东转100元": "T04",
        "AA聚餐100元 user_002 user_003": "T05",
        "看看异常交易": "B02",
        "查看理财持仓收益": "W06",
        "申请一张虚拟卡": "C01",
        "把额度调低到900元": "C02",
        "限制网购": "C03",
        "解除网购限制": "C04",
        "解除挂失": "C06",
        "修改银行卡密码1234": "C08",
        "提醒我视频会员续费": "S02",
        "先预留1000元": "X02",
        "生日订鲜花": "X03",
        "生日订蛋糕": "X04",
        "卡被盗刷了": "X05",
    }
    assert {parser.parse(text).capability_id for text in cases} == set(cases.values())
    for text, capability_id in cases.items():
        assert parser.parse(text).capability_id == capability_id


def test_intent_parser_normalizes_common_m2_phrasing_without_slot_corruption() -> None:
    parser = IntentParser()

    phone_transfer = parser.parse("给 13800000001 转 100 元")
    assert phone_transfer.capability_id == "T02"
    assert phone_transfer.slots["phone"] == "13800000001"
    assert phone_transfer.slots["amount_minor"] == 10_000

    comparison = parser.parse("对比理财产品")
    assert comparison.capability_id == "W02"

    loss_report = parser.parse("挂失我的卡")
    assert loss_report.capability_id == "C05"

    stolen_card = parser.parse("我的卡被盗刷了")
    assert stolen_card.capability_id == "X05"

    unfreeze = parser.parse("解冻我的卡")
    assert unfreeze.capability_id == "C07"
    assert unfreeze.slots["action"] == "unfreeze"

    redemption = parser.parse("赎回订单 order_123")
    assert redemption.capability_id == "W05"
    assert redemption.slots["order_id"] == "order_123"
    redemption_uuid = parser.parse("赎回订单 order_abc123")
    assert redemption_uuid.slots["order_id"] == "order_abc123"

    named_transfer = parser.parse("给张三转 100 元")
    assert named_transfer.capability_id == "T01"
    assert named_transfer.slots["recipient_name"] == "张三"


def test_agent_executes_card_application_and_scheduled_transfer_routes(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    card = agent.handle("user_001", "card-route", "申请一张虚拟卡")
    assert card.capability_id == "C01" and card.kind == "confirmation"
    card_result = agent.confirm("user_001", card.record_id, card.display_hash, card.data["receipt_id"])
    assert card_result.data["status"] == "active"

    scheduled = agent.handle("user_001", "schedule-route", "每月给房东转100元")
    assert scheduled.capability_id == "T04" and scheduled.kind == "confirmation"
    scheduled_result = agent.confirm("user_001", scheduled.record_id, scheduled.display_hash,
                                     scheduled.data["receipt_id"])
    assert scheduled_result.data["schedule_id"]


def test_agent_phone_transfer_and_product_comparison_reach_business_results(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    phone_transfer = agent.handle("user_001", "phone-transfer", "给 13800000001 转 100 元")
    assert phone_transfer.kind == "confirmation"
    assert phone_transfer.capability_id == "T02"
    assert phone_transfer.data["display"]
    completed = agent.confirm("user_001", phone_transfer.record_id, phone_transfer.display_hash)
    assert completed.data["transfer_result"]["status"] == "succeeded"
    assert completed.data["transfer_result"]["amount_minor"] == 10_000

    comparison = agent.handle("user_001", "compare-products", "对比理财产品")
    assert comparison.kind == "facts"
    assert comparison.capability_id == "W02"
    assert len(comparison.data["products"]) >= 2


def test_agent_never_accepts_or_persists_pin_in_chat(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'pin-chat.db'}")
    load_seed(database)
    agent = AgentOrchestrator(database)
    response = agent.handle("user_001", "pin-chat", "修改银行卡密码 1234")
    assert response.kind == "clarification"
    assert "App" in response.message
    from agentbank.db import ConversationState
    with database.sessions() as session:
        state = session.get(ConversationState, ("user_001", "pin-chat"))
        assert state is not None
        assert "1234" not in state.recent_user_messages_json


def test_agent_completes_remaining_m2_slot_followups(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    first = agent.handle("user_001", "redeem-followup", "赎回理财")
    assert first.kind == "clarification" and first.capability_id == "W05"
    second = agent.handle("user_001", "redeem-followup", "订单 order_abc123")
    assert second.kind == "confirmation" and second.data["payload"]["order_id"] == "order_abc123"

    scheduled = agent.handle("user_001", "schedule-followup", "每月给房东转账")
    assert scheduled.kind == "clarification" and scheduled.capability_id == "T04"
    completed = agent.handle("user_001", "schedule-followup", "100元")
    assert completed.kind == "confirmation" and completed.capability_id == "T04"


def test_agent_can_select_only_redeemable_order_from_positions(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    from agentbank.bank.capabilities_full import CapabilityService
    direct = CapabilityService(agent.database)
    direct.execute("user_001", "W03", {"answers": [2] * 10}, "redeem-select-risk-direct")
    direct.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 1_000,
                                         "assurance": "mfa"}, "redeem-select-buy-direct")
    response = agent.handle("user_001", "redeem-select", "赎回R1理财")
    assert response.kind == "confirmation"
    assert response.capability_id == "W05"
    assert response.data["payload"]["order_id"]


def test_agent_uses_llm_planner_for_intent_outside_deterministic_parser(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'planner-agent.db'}")
    load_seed(database)

    def transport(config: ProviderConfig, role: str, payload: dict[str, object]) -> dict[str, object]:
        if role == "planner":
            return {"intent": "subscription_reminder", "capability_id": "S02",
                    "slots": {"subscription_id": "sub_video", "days": 3},
                    "missing": [], "ambiguities": [], "suggested_next_action": "execute"}
        return {"message": "已处理", "fragments": []}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"), None,
                         transport=transport)
    response = AgentOrchestrator(database, llm=gateway).handle("user_001", "llm-route", "提醒我续费")
    assert response.capability_id == "S02"
    assert response.kind == "confirmation"


def test_agent_uses_planner_without_extra_classifier_call(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'single-plan-call.db'}")
    load_seed(database)
    roles: list[str] = []

    def transport(config: ProviderConfig, role: str, payload: dict[str, object]) -> dict[str, object]:
        roles.append(role)
        return {"intent": "subscription_reminder", "capability_id": "S02",
                "slots": {"subscription_id": "sub_video", "days": 3},
                "missing": [], "ambiguities": [], "suggested_next_action": "execute"}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"), None,
                         transport=transport)
    AgentOrchestrator(database, llm=gateway).handle("user_001", "single-plan-call", "提醒我续费")
    assert roles == ["planner"]


def test_agent_uses_llm_reply_role_for_structured_fact_response(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'reply-agent.db'}")
    load_seed(database)

    def transport(config: ProviderConfig, role: str, payload: dict[str, object]) -> dict[str, object]:
        if role == "reply":
            return {"message": "模型根据事实生成的结果", "fragments": [{"type": "fact", "label": "可用余额",
                                                                    "value_minor": 500_000, "currency": "CNY"}]}
        return {"intent": "unknown", "confidence": 0.1}

    gateway = LLMGateway(database, ProviderConfig("primary", "https://primary.invalid", "secret", "model-a"), None,
                         transport=transport)
    response = AgentOrchestrator(database, llm=gateway).handle("user_001", "reply-route", "查余额")
    assert response.message == "模型根据事实生成的结果"
    assert response.fragments[0]["value_minor"] == 500_000
