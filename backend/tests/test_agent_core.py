from pathlib import Path

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.db import Database
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
    assert complete.kind == "facts"
    assert complete.data["level"] == "C1"


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
