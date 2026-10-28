from pathlib import Path

import pytest

from agentbank.bank.service import TransferService
from agentbank.db import Database, PlanNodeRecord, PlanRecord, ReceiptRecord
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService


def test_transfer_recomputes_action_hash_before_commit(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'auth.db'}")
    load_seed(database)
    transfer = TransferService(database)
    prepared = transfer.prepare("user_001", "房东", 2_000)
    with database.sessions.begin() as session:
        node = session.query(PlanNodeRecord).filter(PlanNodeRecord.plan_id == prepared.plan_id).one()
        node.args_json = node.args_json.replace('"amount_minor": 2000', '"amount_minor": 9999')
    with pytest.raises(DomainError, match="HASH_MISMATCH"):
        transfer.confirm("user_001", prepared.plan_id, prepared.receipt_id, prepared.display_hash)


def test_plan_revision_invalidates_old_receipt_and_reprepares(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'revision.db'}")
    load_seed(database)
    planner = BusinessPlanService(database)
    original = planner.create_birthday_plan("user_001", "event_birthday", 100_000)
    revised = planner.revise_birthday_plan("user_001", original.plan_id, {"reserve_minor": 80_000})
    assert revised.revision == 2
    with database.sessions() as session:
        old = session.get(PlanRecord, original.plan_id)
        assert old is not None and old.status == "invalidated"
        receipts = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == original.plan_id).all()
        assert all(receipt.status == "invalidated" for receipt in receipts)
