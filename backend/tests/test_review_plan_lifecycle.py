from datetime import date, timedelta

from sqlalchemy import select
from agentbank.clock import business_date

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Account, Database, Mandate, MerchantOrder, PlanRecord, ReceiptRecord, Reservation, Operation, Event
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService


def prepared():
    db = Database('sqlite://')
    load_seed(db)
    with db.sessions.begin() as session:
        session.get(Event, 'event_birthday').event_date = (date.fromisoformat(business_date()) + timedelta(days=14)).isoformat()
    service = BusinessPlanService(db)
    plan = service.create_birthday_plan('user_001', 'event_birthday', 100000)
    with db.sessions() as session:
        receipt = session.scalar(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan.plan_id))
        result = service.execute_plan('user_001', plan.plan_id, receipt.id, receipt.display_hash)
    return db, service, plan, result


def test_scheduled_birthday_can_cancel_and_release_everything():
    db, service, plan, result = prepared()
    assert result['status'] == 'waiting_trigger'
    assert service.cancel('user_001', plan.plan_id)['status'] == 'cancelled'
    cap = CapabilityService(db)
    cap.clock.advance_days(40)
    assert cap.dispatch_due('user_001') == []
    with db.sessions() as session:
        assert all(o.status == 'cancelled' for o in session.scalars(select(MerchantOrder)).all())
        assert all(m.status == 'revoked' for m in session.scalars(select(Mandate)).all())
        assert session.scalar(select(Account).where(Account.user_id == 'user_001')).available_minor == 500000
        assert session.scalar(select(Reservation)).status == 'released'
        assert not session.scalars(select(Operation)).all()


def test_pause_blocks_due_dispatch_then_resume_executes_once_and_finishes_parent():
    db, service, plan, result = prepared()
    assert result['status'] == 'waiting_trigger'
    service.pause('user_001', plan.plan_id)
    cap = CapabilityService(db)
    cap.clock.advance_days(12)
    assert cap.dispatch_due('user_001') == []
    assert service.resume('user_001', plan.plan_id)['status'] == 'waiting_trigger'
    assert len(cap.dispatch_due('user_001')) == 2
    assert cap.dispatch_due('user_001') == []
    with db.sessions() as session:
        assert session.get(PlanRecord, plan.plan_id).status == 'succeeded'
        assert session.query(Operation).count() == 2
        assert session.scalar(select(Reservation)).status in {'released', 'consumed'}



def test_refresh_does_not_complete_partial_plan_with_unfinished_nodes():
    from agentbank.db import PlanNodeRecord
    db, service, plan, _ = prepared()
    with db.atomic() as session:
        parent = session.get(PlanRecord, plan.plan_id)
        parent.status = 'partially_completed'
        for node in session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan.plan_id)).all():
            node.status = 'pending'
        BusinessPlanService.refresh_waiting_plans(session)
        assert parent.status == 'partially_completed'
