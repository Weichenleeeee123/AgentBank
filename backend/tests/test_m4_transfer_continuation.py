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
                         followup, 'payee_landlord', expect_clarification=followup['slots'].get('amount_minor') != 2000)


@pytest.mark.parametrize('text,route,slots,payee', [
    ('给13800000001转账', 'T02', {'phone': '13800000001'}, 'payee_zhang_colleague'),
    ('按房东备注转账', 'T03', {'alias': '房东'}, 'payee_landlord'),
])
@pytest.mark.parametrize('followup_text', ['20元', '转20元'])
def test_phone_and_alias_pending_preserve_recipient_across_model_routes(monkeypatch, text, route, slots, payee, followup_text):
    followup = {'capability_id': 'T01', 'slots': {'amount_minor': 2000}, 'missing': ['recipient_name']}
    _assert_continuation(monkeypatch, text, route, slots, followup, payee, followup_text)


def test_user_can_explicitly_choose_a_different_recipient_in_followup(monkeypatch):
    followup = {'capability_id': 'T02', 'slots': {'amount_minor': 2000, 'phone': '13800000001'}, 'missing': []}
    _assert_continuation(monkeypatch, '给房东转账', 'T01', {'recipient_name': '房东'},
                         followup, 'payee_zhang_colleague', '给13800000001转20元')


def _assert_continuation(monkeypatch, first_text, first_route, first_slots, followup, expected_payee,
                         followup_text='20元', expect_clarification=False):
    database = Database('sqlite://')
    load_seed(database)
    def transport(config, role, payload):
        if role == 'fraud':
            return {'category': 'safe', 'confidence': 1.0}
        if role == 'planner':
            if not payload['context'].get('pending_intent'):
                result = {'intent': 'transfer', 'capability_id': first_route, 'slots': dict(first_slots),
                          'missing': ['amount_minor'], 'ambiguities': []}
            else:
                result = {'intent': 'transfer', 'ambiguities': [], **followup, 'slots': dict(followup['slots'])}
            if 'phone' in result['slots'] and 'phone_ref_1' in payload['text']:
                result['slots']['phone'] = 'phone_ref_1'
            return result
        return {'message': '请核对结果。', 'fragments': []}
    # Exercise grounding/validation too: bypassing plan_intent hid fabricated
    # amounts and recipients that the real gateway must reject.
    gateway = LLMGateway(database, ProviderConfig('fake', 'https://example.invalid', 'unused', 'fake'), None,
                         transport=transport)
    agent = AgentOrchestrator(database, llm=gateway)
    first = agent.handle('user_001', 'same-chat', first_text, message_id='first')
    assert first.kind == 'clarification'
    second = agent.handle('user_001', 'same-chat', followup_text, message_id='second')
    if expect_clarification:
        assert second.kind == 'clarification'
        assert second.record_id is None
        with database.sessions() as session:
            assert not session.scalars(select(LedgerEntry)).all()
        return
    assert second.kind == 'confirmation'
    with database.sessions() as session:
        record = session.get(CapabilityRecord, second.record_id)
        plan_id = json.loads(record.data_json)['plan_id']
        node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id))
        arguments = json.loads(node.args_json)
        assert arguments['recipient_id'] == expected_payee
        assert arguments['amount_minor'] == 2000
        assert not session.scalars(select(LedgerEntry)).all()
