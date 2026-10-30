from pathlib import Path

import pytest

from agentbank.db import Database, PlanRecord, ReceiptRecord
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService


def test_plan_execution_uses_dependency_order_not_database_id(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'dag-order.db'}")
    load_seed(database)
    service = BusinessPlanService(database)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    with database.sessions() as session:
        record = session.get(PlanRecord, plan.plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one()
        assert record is not None
        result = service.execute_plan("user_001", plan.plan_id, receipt.id, record.display_hash)
    assert [node["node_id"] for node in result["nodes"]] == ["event", "quote_flower", "quote_cake", "reserve", "flowers", "cake"]


def test_paused_plan_cannot_dispatch_until_resumed(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'dag-pause.db'}")
    load_seed(database)
    service = BusinessPlanService(database)
    plan = service.create_birthday_plan("user_001", "event_birthday", 100_000)
    service.pause("user_001", plan.plan_id)
    with database.sessions() as session:
        record = session.get(PlanRecord, plan.plan_id)
        receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one()
        assert record is not None
        with pytest.raises(DomainError, match="PLAN_STATE_INVALID"):
            service.execute_plan("user_001", plan.plan_id, receipt.id, record.display_hash)
