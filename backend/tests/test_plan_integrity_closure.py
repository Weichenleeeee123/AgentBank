"""Regression tests for revision, dependency recovery and arbitrary node IDs."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.contracts import PlanNode
from agentbank.db import Account, Database, Event, Mandate, PlanRecord, ReceiptRecord, Reservation
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.workflow.planner import BusinessPlanService


@pytest.fixture()
def plans(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'plans.db'}")
    load_seed(db)
    with db.sessions.begin() as session:
        session.get(Event, 'event_birthday').event_date = (date.today() + timedelta(days=14)).isoformat()
    return db, BusinessPlanService(db)


def execute(db, service, plan):
    with db.sessions() as session:
        receipt = session.scalar(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan.plan_id))
    return service.execute_plan('user_001', plan.plan_id, receipt.id, receipt.display_hash)


def test_revision_retires_old_reservations_and_authority(plans):
    db, service = plans
    original = service.create_birthday_plan('user_001', 'event_birthday', 100000)
    execute(db, service, original)
    revised = service.revise_birthday_plan('user_001', original.plan_id, {'reserve_minor': 200000})
    execute(db, service, revised)
    service.cancel('user_001', revised.plan_id)
    with db.sessions() as session:
        assert session.get(Account, 'acc_001').available_minor == 500000
        assert all(r.status != 'active' for r in session.scalars(select(Reservation)))
        assert all(m.status == 'revoked' for m in session.scalars(select(Mandate)))


def test_failed_revision_preserves_old_plan_and_money(plans, monkeypatch):
    db, service = plans
    original = service.create_birthday_plan('user_001', 'event_birthday', 100000)
    execute(db, service, original)
    def fail(*args, **kwargs):
        raise DomainError('TEST_FAILURE')
    monkeypatch.setattr(service, 'create_birthday_plan', fail)
    with pytest.raises(DomainError, match='TEST_FAILURE'):
        service.revise_birthday_plan('user_001', original.plan_id, {})
    with db.sessions() as session:
        assert session.get(PlanRecord, original.plan_id).status == 'waiting_trigger'
        assert session.get(Account, 'acc_001').available_minor == 400000
        assert all(m.status == 'active' for m in session.scalars(select(Mandate)))


@pytest.mark.parametrize('node_id', ['event', 'quote_flower', 'quote_cake'])
def test_generic_node_name_does_not_select_template_handler(plans, node_id):
    db, service = plans
    plan = service.create_proposed_plan('user_001', 'reserve', [
        PlanNode(id=node_id, tool='X02', args={'amount_minor': 1000}, idempotency_key='model')])
    result = execute(db, service, plan)
    assert result['status'] == 'succeeded'
    assert result['nodes'][0]['data']['reservation_id']


def test_resume_restores_consumed_dependency_output(plans, monkeypatch):
    db, service = plans
    plan = service.create_proposed_plan('user_001', 'reserve and buy', [
        PlanNode(id='hold', tool='X02', args={'amount_minor': 10000}, idempotency_key='hold'),
        PlanNode(id='buy', tool='X03', args={'delivery_date': (date.today() + timedelta(days=14)).isoformat(),
                 'reservation_id': {'$ref': 'hold.reservation_id'}}, depends_on=['hold'], idempotency_key='buy')])
    original = CapabilityService.execute
    def pause_after_reserve(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[1] == 'X02':
            service.pause('user_001', plan.plan_id)
        return result
    with monkeypatch.context() as m:
        m.setattr(CapabilityService, 'execute', pause_after_reserve)
        assert execute(db, service, plan)['status'] == 'paused'
    service.resume('user_001', plan.plan_id)
    result = execute(db, service, plan)
    assert result['status'] == 'waiting_trigger'
    with db.sessions() as session:
        assert len(session.scalars(select(Reservation)).all()) == 1


@pytest.mark.parametrize('tool,args', [('T01', {'recipient_name': '房东', 'amount_minor': 1000}),
    ('T02', {'phone': '13800138000', 'amount_minor': 1000}),
    ('T03', {'alias': '房东', 'amount_minor': 1000})])
def test_transfer_dag_requires_dedicated_transfer_authorization(plans, tool, args):
    db, service = plans
    with pytest.raises(DomainError, match='PLAN_TRANSFER_REQUIRES_SINGLE_FLOW'):
        service.create_proposed_plan('user_001', 'transfer', [
            PlanNode(id='pay', tool=tool, args=args, idempotency_key='pay')])
    with db.sessions() as session:
        assert session.get(Account, 'acc_001').available_minor == 500000
        assert not session.scalars(select(PlanRecord)).all()
