from pathlib import Path

from agentbank.db import Database, PlanRecord, ReceiptRecord
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
    assert executed["status"] in {"succeeded", "partially_completed"}
    graph = service.graph(plan.plan_id, "user_001")
    assert any(node["status"] == "succeeded" for node in graph["nodes"])
