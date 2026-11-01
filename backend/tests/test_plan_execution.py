from pathlib import Path
import pytest

from agentbank.db import Database, PlanNodeRecord, PlanRecord, ReceiptRecord
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService


def test_birthday_plan_can_execute_nodes_and_update_graph(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'plan-exec.db'}")
    load_seed(database)
    service = BusinessPlanService(database)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    with database.sessions() as session:
        plan_record = session.get(PlanRecord, plan.plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one()
        assert plan_record is not None
        executed = service.execute_plan("user_001", plan.plan_id, receipt.id, plan_record.display_hash)
    assert executed["status"] == "waiting_trigger"
    assert [node["node_id"] for node in executed["nodes"]] == [
        "event", "quote_flower", "quote_cake", "reserve", "flowers", "cake"
    ]
    graph = service.graph(plan.plan_id, "user_001")
    assert any(node["status"] == "succeeded" for node in graph["nodes"])


def test_plan_action_hash_detects_node_mutation_before_execution(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'plan-hash.db'}")
    load_seed(database)
    service = BusinessPlanService(database)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    with database.sessions.begin() as session:
        record = session.get(PlanRecord, plan.plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one()
        node = session.query(PlanNodeRecord).filter(PlanNodeRecord.plan_id == plan.plan_id,
                                                     PlanNodeRecord.tool == "funds.reserve").one()
        assert record is not None
        node.args_json = node.args_json.replace("100000", "90000")
    with pytest.raises(DomainError, match="HASH_MISMATCH"):
        service.execute_plan("user_001", plan.plan_id, receipt.id, record.display_hash)
