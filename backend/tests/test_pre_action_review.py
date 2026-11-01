import pytest

from agentbank.review.pre_action import PreActionReviewService
from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.db import AuditLog, CapabilityRecord, Database, Payee, Product
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.bank.service import TransferService
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


def test_review_resolves_subject_and_exposes_all_report_sections():
    result = PreActionReviewService().review("星河理财", action="transfer")

    assert result["decision"] == "pass"
    assert result["subject"]["name"] == "星河模拟理财有限公司"
    assert result["registration"]["status"] == "active"
    assert result["registration"]["record_id"]
    assert result["qualification"]["status"] == "found"
    signals = {item["key"]: item for item in result["signals"]}
    assert set(signals) == {"risk", "finance", "credit", "reputation"}
    assert all(signals[key]["status"] == "simulated" for key in signals)
    assert all(signals[key]["record_id"] for key in signals)
    assert all("未提供" not in signals[key]["value"] for key in signals)
    assert result["evidence_sources"]
    assert result["gaps"]
    assert result["questions"]


def test_transfer_and_investment_reviews_show_action_specific_simulated_evidence():
    service = PreActionReviewService()
    transfer = service.review("星河模拟理财有限公司", action="transfer")
    investment = service.review("星河模拟理财有限公司", action="investment")

    assert "收款户名" in "；".join(transfer["questions"])
    assert "产品登记编码" in "；".join(investment["gaps"])
    assert all("SIM-" in source for source in investment["evidence_sources"])


def test_unknown_entity_is_blocked_before_payment():
    result = PreActionReviewService().review("某某财富管理公司", action="investment")

    assert result["decision"] == "block"
    assert result["qualification"]["status"] == "not_found"
    assert any("主体" in gap for gap in result["gaps"])


def test_investment_review_includes_product_suitability_angle():
    result = PreActionReviewService().review("星河理财", action="investment", product_id="product_R3",
                                             product={"risk_level": "R3", "redemption_days": 1})

    assert result["product"]["risk_level"] == "R3"
    assert any("风险" in question or "赎回" in question for question in result["questions"])


def test_company_transfer_is_stopped_before_confirmation_when_entity_is_unmatched():
    database = Database("sqlite://")
    load_seed(database)
    response = AgentOrchestrator(database).handle(
        "user_001", "review", "给某某财富管理有限公司转账100元")

    assert response.data["pre_action_review"]["decision"] == "block"
    with database.sessions() as session:
        assert session.query(CapabilityRecord).count() == 0


def test_investment_confirmation_contains_pre_action_review():
    database = Database("sqlite://")
    load_seed(database)
    response = AgentOrchestrator(database).handle("user_001", "review", "申购R3理财100元")

    assert response.kind == "confirmation"
    assert response.data["pre_action_review"]["decision"] == "pass"
    assert response.data["pre_action_review"]["product"]["term_days"] == 30
    assert any(item["label"] == "行动前审查 · 主体" for item in response.data["display"])


def test_direct_investment_capability_cannot_bypass_entity_review():
    database = Database("sqlite://")
    load_seed(database)
    with database.sessions.begin() as session:
        product = session.get(Product, "product_R3")
        product.issuer_name = "陌生演示理财有限公司"

    with pytest.raises(DomainError, match="ACTION_REVIEW_BLOCKED"):
        CapabilityService(database, enforce_prepare=True).execute(
            "user_001", "W04", {"product_id": "product_R3", "amount_minor": 10_000}, "unmatched-issuer")

    with database.sessions() as session:
        assert session.query(CapabilityRecord).count() == 0


def test_known_corporate_payee_is_reviewed_and_added_to_authorized_display():
    database = Database("sqlite://")
    load_seed(database)
    prepared = TransferService(database).prepare_for_payee(
        "user_001", "payee_simulated_corporate", 10_000)

    assert prepared.pre_action_review["decision"] == "pass"
    assert any(item["label"] == "行动前审查 · 核验缺口" for item in prepared.display)


def test_blocked_corporate_transfer_persists_review_audit_before_rejection():
    database = Database("sqlite://")
    load_seed(database)
    with database.sessions.begin() as session:
        payee = session.get(Payee, "payee_simulated_corporate")
        payee.display_name = "陌生模拟企业有限公司"

    with pytest.raises(DomainError, match="ACTION_REVIEW_BLOCKED"):
        TransferService(database).prepare_for_payee("user_001", "payee_simulated_corporate", 10_000)

    with database.sessions() as session:
        audit = session.query(AuditLog).filter_by(event_type="pre_action_review.blocked").one()
        assert audit.subject_id == "user_001"
