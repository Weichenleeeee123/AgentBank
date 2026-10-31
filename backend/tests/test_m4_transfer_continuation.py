import json

import pytest
from sqlalchemy import select

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.db import CapabilityRecord, Database, LedgerEntry, PlanNodeRecord
from agentbank.llm.gateway import LLMGateway, ProviderConfig
from agentbank.seed.loader import load_seed


@pytest.mark.parametrize('followup', [
    {'capability_id': 'T03', 'slots': {'amount_minor': 2000}, 'missing': ['alias']},
    {'capability_id': 'T03', 'slots': {'amount_minor': 2000, 'alias': '房东'}, 'missing': []},
    {'capability_id': 'T03', 'slots': {'amount_minor': 3000, 'alias': '张三'}, 'missing': []},
    {'capability_id': 'T02', 'slots': {'amount_minor': 2000, 'phone': '13800000002'}, 'missing': []},
])
def test_name_transfer_continues_when_planner_switches_transfer_route(monkeypatch, followup):
    _assert_continuation(monkeypatch, '给房东转账', 'T01', {'recipient_name': '房东'},
                         followup, 'payee_landlord')


@pytest.mark.parametrize('text,route,slots,payee', [
    ('给13800000001转账', 'T02', {'phone': '13800000001'}, 'payee_zhang_colleague'),
    ('给房租转账', 'T03', {'alias': '房东'}, 'payee_landlord'),
])
@pytest.mark.parametrize('followup_text', ['20元', '转20元'])
def test_phone_and_alias_pending_preserve_recipient_across_model_routes(monkeypatch, text, route, slots, payee, followup_text):
    followup = {'capability_id': 'T01', 'slots': {'amount_minor': 2000}, 'missing': ['recipient_name']}
    _assert_continuation(monkeypatch, text, route, slots, followup, payee, followup_text)


def test_user_can_explicitly_choose_a_different_recipient_in_followup(monkeypatch):
    followup = {'capability_id': 'T03', 'slots': {'amount_minor': 2000, 'alias': '房东'}, 'missing': []}
    _assert_continuation(monkeypatch, '给房东转账', 'T01', {'recipient_name': '房东'},
                         followup, 'payee_zhang_colleague', '给13800000001转20元')


def _assert_continuation(monkeypatch, first_text, first_route, first_slots, followup, expected_payee, followup_text='20元'):
    database = Database('sqlite://')
    load_seed(database)
    gateway = LLMGateway(database, ProviderConfig('fake', 'https://example.invalid', 'unused', 'fake'), None)
    plans = iter([
        {'intent': 'transfer', 'capability_id': first_route, 'slots': first_slots,
         'missing': ['amount'], 'ambiguities': []},
        {'intent': 'transfer', 'ambiguities': [], **followup},
    ])
    monkeypatch.setattr(gateway, 'plan_intent', lambda *args, **kwargs: next(plans))
    monkeypatch.setattr(gateway, 'classify_fraud', lambda *args, **kwargs: {'category': 'safe', 'confidence': 1.0})
    agent = AgentOrchestrator(database, llm=gateway)
    first = agent.handle('user_001', 'same-chat', first_text, message_id='first')
    assert first.kind == 'clarification'
    second = agent.handle('user_001', 'same-chat', followup_text, message_id='second')
    assert second.kind == 'confirmation'
    with database.sessions() as session:
        record = session.get(CapabilityRecord, second.record_id)
        plan_id = json.loads(record.data_json)['plan_id']
        node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id))
        arguments = json.loads(node.args_json)
        assert arguments['recipient_id'] == expected_payee
        assert arguments['amount_minor'] == 2000
        assert not session.scalars(select(LedgerEntry)).all()
