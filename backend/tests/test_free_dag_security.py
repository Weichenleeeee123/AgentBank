from pathlib import Path

import pytest

from agentbank.contracts import PlanNode
from agentbank.db import Database, PlanRecord, ReceiptRecord
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService
from agentbank.planning.validator import PlanValidator


def setup(tmp_path: Path) -> tuple[Database, BusinessPlanService]:
    database = Database(f"sqlite:///{tmp_path / 'free-dag-security.db'}")
    load_seed(database)
    return database, BusinessPlanService(database)


def test_free_dag_accepts_a_typed_reference_and_executes_it(tmp_path: Path) -> None:
    database, service = setup(tmp_path)
    plan = service.create_proposed_plan("user_001", "reserve available balance", [
        PlanNode(id="balance", tool="Q01", args={}, idempotency_key="model-balance"),
        PlanNode(id="reserve", tool="X02", args={"amount_minor": {"$ref": "balance.available_minor"}},
                 depends_on=["balance"], compensation="funds.release", idempotency_key="model-reserve"),
    ])

    with database.sessions() as session:
        plan_record = session.get(PlanRecord, plan.plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one()
        assert plan_record is not None
        assert plan.nodes[1].args["amount_minor"] == 500_000
        assert {"label": "reserve·金额", "value": "¥5000.00"} in plan.display
        display_hash = plan_record.display_hash
        receipt_id = receipt.id

    result = service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)
    assert result["status"] == "succeeded"
    assert result["nodes"][-1]["data"]["amount_minor"] == 500_000


def test_free_dag_rejects_server_owned_authorization_fields(tmp_path: Path) -> None:
    _database, _service = setup(tmp_path)
    with pytest.raises(DomainError, match="PLAN_SERVER_FIELD"):
        PlanValidator.validate_proposal([{
            "id": "subscribe", "tool": "W04",
            "args": {"product_id": "product_R1", "amount_minor": 10_000, "assurance": "mfa"},
            "depends_on": [], "compensation": "investment.order.cancel",
            "timeout_ms": 5_000, "idempotency_key": "model-subscribe",
        }])


def test_free_dag_can_wire_a_server_generated_reservation_id_to_order(tmp_path: Path) -> None:
    database, service = setup(tmp_path)
    plan = service.create_proposed_plan("user_001", "reserve then order", [
        PlanNode(id="reserve", tool="X02", args={"amount_minor": 100_000},
                 compensation="funds.release", idempotency_key="model-reserve"),
        PlanNode(id="order", tool="X03", args={
            "delivery_date": "2026-11-15", "reservation_id": {"$ref": "reserve.reservation_id"}},
                 depends_on=["reserve"], compensation="merchant.cancel_and_release",
                 idempotency_key="model-order"),
    ])

    with database.sessions() as session:
        plan_record = session.get(PlanRecord, plan.plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one()
        assert plan_record is not None and receipt is not None
        receipt_id, display_hash = receipt.id, plan_record.display_hash

    result = service.execute_plan("user_001", plan.plan_id, receipt_id, display_hash)
    assert result["status"] == "waiting_trigger"
    assert [item["node_id"] for item in result["nodes"]] == ["reserve", "order"]
    assert result["nodes"][1]["data"]["order_id"]
