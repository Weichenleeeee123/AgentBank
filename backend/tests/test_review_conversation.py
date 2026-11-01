import json

import pytest

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.db import CapabilityRecord, Database
from agentbank.errors import DomainError
from agentbank.llm.gateway import LLMGateway, ProviderConfig
from agentbank.seed.loader import load_seed


@pytest.mark.parametrize('text,action,cap,slots', [
    ('金额换成30元', 'revise', 'T01', {'recipient_name': '房东', 'amount_minor': 3000}),
    ('别取消这笔转账', 'keep', None, {}),
    ('取消我的视频订阅', 'new', 'S03', {'subscription_id': 'sub_video'}),
    ('再给房东转30元，原来的保留', 'new', 'T01', {'recipient_name': '房东', 'amount_minor': 3000}),
    ('这笔不用办了', 'cancel', None, {}),
])
def test_semantic_task_control(monkeypatch, text, action, cap, slots):
    db = Database('sqlite://')
    load_seed(db)
    def transport(config, role, payload):
        if role == 'planner':
            if payload['text'] == '给房东转20元':
                return {'intent': 'transfer', 'capability_id': 'T01', 'slots': {'recipient_name': '房东', 'amount_minor': 2000}, 'missing': []}
            return {'intent': 'control' if cap is None else 'business', 'capability_id': cap,
                    'slots': slots, 'missing': [], 'task_action': action, 'task_reference': text}
        if role == 'fraud':
            return {'category': 'safe', 'confidence': 1.0}
        return {'message': '请核对。', 'fragments': []}
    agent = AgentOrchestrator(db, llm=LLMGateway(db, ProviderConfig('test', 'https://example.invalid', 'x', 'x'), None, transport=transport))
    first = agent.handle('user_001', 'chat', '给房东转20元')
    second = agent.handle('user_001', 'chat', text)
    with db.sessions() as session:
        old = session.get(CapabilityRecord, first.record_id)
        assert old.status == ('invalidated' if action == 'revise' else 'cancelled' if action == 'cancel' else 'awaiting_confirmation')
    if action in {'revise', 'cancel'}:
        with pytest.raises(DomainError):
            agent.confirm('user_001', first.record_id, first.display_hash, first.data['receipt_id'])
        assert first.record_id in second.data['invalidated_record_ids']
    if action == 'new':
        assert second.capability_id == cap


@pytest.mark.parametrize('text', ['不要取消这笔转账', '取消我的视频订阅', '取消订阅但保留转账'])
def test_offline_cancel_does_not_target_unrelated_transfer(text):
    db = Database('sqlite://')
    load_seed(db)
    agent = AgentOrchestrator(db, llm=LLMGateway(db, None, None))
    first = agent.handle('user_001', 'chat', '给房东转20元')
    agent.handle('user_001', 'chat', text)
    with db.sessions() as session:
        old = session.get(CapabilityRecord, first.record_id)
        assert old.status == 'awaiting_confirmation', json.loads(old.data_json)
