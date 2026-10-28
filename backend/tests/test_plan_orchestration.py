from pathlib import Path

from agentbank.db import Database
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService


def test_birthday_plan_has_dag_dependencies_authorization_and_compensation(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'plans.db'}")
    load_seed(database)
    service = BusinessPlanService(database)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    assert plan.status == "awaiting_confirmation"
    assert [node.tool for node in plan.nodes] == ["events.lookup", "funds.reserve", "merchant.order.create", "merchant.order.create"]
    assert plan.nodes[1].depends_on == ["event"]
    assert plan.nodes[2].compensation == "funds.release"
    assert plan.nodes[3].depends_on == ["reserve"]
    graph = service.graph(plan.plan_id, "user_001")
    assert graph["nodes"][1]["authorization"] == "confirm"
    assert graph["nodes"][2]["compensation"] == "funds.release"
