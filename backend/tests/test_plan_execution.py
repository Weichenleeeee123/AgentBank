from pathlib import Path

from agentbank.db import Database
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService


def test_birthday_plan_can_execute_nodes_and_update_graph(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'plan-exec.db'}")
    load_seed(database)
    service = BusinessPlanService(database)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    executed = service.execute_plan("user_001", plan.plan_id, confirmed=True)
    assert executed["status"] in {"succeeded", "partially_completed"}
    graph = service.graph(plan.plan_id, "user_001")
    assert any(node["status"] == "succeeded" for node in graph["nodes"])
