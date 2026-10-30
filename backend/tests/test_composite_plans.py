import json
from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.contracts import CapabilityResult
from agentbank.db import CapabilityRecord, Database, PlanNodeRecord, PlanRecord, ReceiptRecord
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService
from agentbank.contracts import PlanNode


def setup(tmp_path: Path) -> tuple[Database, BusinessPlanService]:
    database = Database(f"sqlite:///{tmp_path / 'composite-plans.db'}")
    load_seed(database)
    return database, BusinessPlanService(database)


def authorize(database: Database, plan_id: str) -> tuple[str, str]:
    with database.sessions() as session:
        plan = session.get(PlanRecord, plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan_id).one()
        assert plan is not None
        return receipt.id, plan.display_hash


def ensure_risk_profile(database: Database) -> None:
    CapabilityService(database).execute("user_001", "W03", {"answers": [3] * 10}, "test-risk-profile")


def test_composite_templates_are_registered_and_validate_required_slots(tmp_path: Path) -> None:
    _database, service = setup(tmp_path)
    with pytest.raises(DomainError, match="PLAN_TEMPLATE_NOT_FOUND"):
        service.create_composite_plan("user_001", "arbitrary_tool", {})
    with pytest.raises(DomainError, match="PLAN_SLOTS_REQUIRED"):
        service.create_composite_plan("user_001", "query_analysis_report", {})
    with pytest.raises(DomainError, match="PLAN_SLOT_INVALID"):
        service.create_composite_plan("user_001", "investment_review", {"amount_minor": 1000})


def test_composite_plan_is_owner_scoped_and_orders_dependencies(tmp_path: Path) -> None:
    database, service = setup(tmp_path)
    plan = service.create_composite_plan("user_001", "query_analysis_report", {
        "start_month": "2026-01", "end_month": "2026-03", "group_by": ["category"],
        "report": "monthly",
    })
    graph = service.graph(plan.plan_id, "user_001")
    assert [node["logical_id"] for node in graph["nodes"]] == ["transactions", "analysis", "report"]
    assert graph["nodes"][1]["depends_on"] == ["transactions"]
    with pytest.raises(DomainError, match="NOT_OWNER"):
        service.graph(plan.plan_id, "user_002")
    with pytest.raises(DomainError, match="NOT_OWNER"):
        service.execute_plan("user_002", plan.plan_id, *authorize(database, plan.plan_id))


def test_investment_review_passes_recommendations_to_product_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, service = setup(tmp_path)
    ensure_risk_profile(database)
    plan = service.create_composite_plan("user_001", "investment_review", {})
    receipt_id, display_hash = authorize(database, plan.plan_id)
    calls: list[tuple[str, dict]] = []
    original = CapabilityService.execute

    def observed(self, user_id: str, capability_id: str, payload: dict, idempotency_key: str, **kwargs):
        calls.append((capability_id, payload))
        return original(self, user_id, capability_id, payload, idempotency_key, **kwargs)

    monkeypatch.setattr(CapabilityService, "execute", observed)
    result = service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)
    assert result["status"] == "succeeded"
    assert [item[0] for item in calls] == ["W01", "W02"]
    with database.sessions() as session:
        node = session.query(PlanNodeRecord).filter(PlanNodeRecord.plan_id == plan.plan_id,
                                                     PlanNodeRecord.tool == "W02").one()
        first = session.query(CapabilityRecord).filter(CapabilityRecord.user_id == "user_001",
                                                       CapabilityRecord.idempotency_key.endswith("-recommendations")).one()
        recommended = json.loads(first.data_json)["products"]
        assert calls[1][1]["product_ids"] == [item["id"] for item in recommended]
        assert node.status == "succeeded"


def test_query_analysis_report_executes_registered_nodes_and_persists_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = setup(tmp_path)
    plan = service.create_composite_plan("user_001", "query_analysis_report", {
        "start_month": "2026-09", "end_month": "2026-09", "group_by": ["category"],
        "report": "monthly",
    })
    receipt_id, display_hash = authorize(database, plan.plan_id)
    calls: list[str] = []
    original = CapabilityService.execute

    def observed(self, user_id: str, capability_id: str, payload: dict, idempotency_key: str, **kwargs):
        calls.append(capability_id)
        return original(self, user_id, capability_id, payload, idempotency_key, **kwargs)

    monkeypatch.setattr(CapabilityService, "execute", observed)
    result = service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)
    assert result["status"] == "succeeded"
    assert calls == ["Q02", "B01", "B03"]
    graph = service.graph(plan.plan_id, "user_001")
    assert [node["observation"]["status"] for node in graph["nodes"]] == ["succeeded"] * 3
    assert graph["nodes"][0]["observation"]["data"]["transactions"]


def test_composite_resume_recovers_capability_committed_before_node_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = setup(tmp_path)
    ensure_risk_profile(database)
    plan = service.create_composite_plan("user_001", "investment_review", {})
    receipt_id, display_hash = authorize(database, plan.plan_id)
    original = CapabilityService.execute
    crashed = False
    calls: list[str] = []

    def fail_after_record(self, user_id: str, capability_id: str, payload: dict, idempotency_key: str, **kwargs):
        nonlocal crashed
        calls.append(idempotency_key)
        result = original(self, user_id, capability_id, payload, idempotency_key)
        if not crashed:
            crashed = True
            raise RuntimeError("simulated process crash")
        return result

    monkeypatch.setattr(CapabilityService, "execute", fail_after_record)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)
    resumed = service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)
    assert resumed["status"] == "succeeded"
    assert calls.count(f"{plan.plan_id}-recommendations") == 1
    with database.sessions() as session:
        record = session.query(CapabilityRecord).filter(CapabilityRecord.user_id == "user_001",
                                                        CapabilityRecord.idempotency_key == f"{plan.plan_id}-recommendations").one()
        node = session.query(PlanNodeRecord).filter(PlanNodeRecord.plan_id == plan.plan_id,
                                                     PlanNodeRecord.id.endswith("recommendations")).one()
        assert session.query(CapabilityRecord).filter(CapabilityRecord.idempotency_key == record.idempotency_key).count() == 1
        assert node.status == "succeeded"
    graph = service.graph(plan.plan_id, "user_001")
    assert graph["status"] == "succeeded"
    assert graph["nodes"][0]["evidence"]["capability_status"] == "succeeded"


def test_read_only_templates_never_report_scheduled_as_settled(tmp_path: Path) -> None:
    database, service = setup(tmp_path)
    ensure_risk_profile(database)
    plan = service.create_composite_plan("user_001", "investment_review", {})
    receipt_id, display_hash = authorize(database, plan.plan_id)
    result = service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)
    assert result["status"] == "succeeded"
    assert all(node["status"] == "succeeded" for node in result["nodes"])


def test_composite_does_not_claim_scheduled_handler_is_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = setup(tmp_path)
    plan = service.create_composite_plan("user_001", "investment_review", {})
    receipt_id, display_hash = authorize(database, plan.plan_id)

    def scheduled(self, user_id: str, capability_id: str, payload: dict, idempotency_key: str, **kwargs):
        return CapabilityResult(capability_id=capability_id, record_id="scheduled-test", status="scheduled",
                                data={}, evidence={})

    monkeypatch.setattr(CapabilityService, "execute", scheduled)
    result = service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)
    assert result["status"] != "succeeded"
    graph = service.graph(plan.plan_id, "user_001")
    assert graph["nodes"][0]["status"] == "scheduled"
    assert graph["nodes"][1]["status"] == "pending"


def test_model_dag_is_compiled_to_an_immutable_user_authorized_plan(tmp_path: Path) -> None:
    database, service = setup(tmp_path)
    nodes = [PlanNode(id="balance", tool="Q01", args={}, idempotency_key="model-value-is-ignored"),
             PlanNode(id="reserve", tool="X02", args={"amount_minor": 100_000}, depends_on=["balance"],
                      compensation="funds.release", idempotency_key="also-ignored")]

    plan = service.create_proposed_plan("user_001", "birthday preparation", nodes)

    assert plan.status == "awaiting_confirmation"
    assert plan.nodes[0].idempotency_key == f"{plan.plan_id}-balance"
    assert plan.nodes[1].idempotency_key == f"{plan.plan_id}-reserve"
    assert plan.nodes[1].args == {"amount_minor": 100_000, "plan_id": plan.plan_id}
    assert any("reserve" in item["label"] for item in plan.display)
    with database.sessions() as session:
        record = session.get(PlanRecord, plan.plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one()
        assert record is not None and receipt.assurance == "confirm"
        assert receipt.node_ids_json == '["balance", "reserve"]'


def test_model_dag_execution_uses_registered_capabilities_after_plan_confirmation(tmp_path: Path) -> None:
    database, service = setup(tmp_path)
    plan = service.create_proposed_plan("user_001", "birthday preparation", [
        PlanNode(id="reserve", tool="X02", args={"amount_minor": 100_000},
                 compensation="funds.release", idempotency_key="ignored")])
    receipt_id, display_hash = authorize(database, plan.plan_id)

    result = service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)

    assert result["status"] == "succeeded"
    assert result["nodes"][0]["status"] == "reserved"
    with database.sessions() as session:
        receipt = session.get(ReceiptRecord, receipt_id)
        node = session.query(PlanNodeRecord).filter(PlanNodeRecord.plan_id == plan.plan_id).one()
        assert receipt is not None and receipt.status == "consumed"
        assert node.status == "reserved"


def test_model_dag_mfa_requires_otp_before_any_write_executes(tmp_path: Path) -> None:
    database, service = setup(tmp_path)
    ensure_risk_profile(database)
    plan = service.create_proposed_plan("user_001", "invest", [
        PlanNode(id="subscribe", tool="W04", args={"product_id": "product_R1", "amount_minor": 10_000},
                 compensation="investment.order.cancel", idempotency_key="ignored")])
    receipt_id, display_hash = authorize(database, plan.plan_id)
    with pytest.raises(DomainError, match="MFA_REQUIRED"):
        service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)

    prepared = service.confirm_plan("user_001", plan.plan_id, receipt_id, display_hash)
    assert prepared["status"] == "awaiting_mfa"
    with database.sessions() as session:
        assert session.query(CapabilityRecord).filter(CapabilityRecord.idempotency_key.endswith("-subscribe")).count() == 0
        from agentbank.db import Challenge
        challenge = session.query(Challenge).filter(Challenge.receipt_id == receipt_id).one()
        otp = challenge.delivery_code
    assert otp is not None
    result = service.verify_plan("user_001", plan.plan_id, otp)
    assert result["status"] == "succeeded"
