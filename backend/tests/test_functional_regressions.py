from pathlib import Path
from datetime import datetime, timezone

import pytest

from agentbank.agent.intent import IntentParser
from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.auth.devices import DeviceService
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database, MerchantOrder, Transaction, CapabilityRecord
from agentbank.llm.gateway import LLMGateway, ProviderConfig
from agentbank.seed.loader import load_seed
from agentbank.clock import BusinessClock
from agentbank.errors import DomainError
import json


@pytest.mark.parametrize(('text', 'capability', 'amount'), [
    ('每月1日给房东转10元，从2026-11-01开始', 'T04', 1000),
    ('从2026年11月1日起，每月给房东转十元', 'T04', 1000),
    ('每月31号给房东转10.25元', 'T04', 1025),
    ('给13800138000转20元', 'T02', 2000),
    ('申购R1理财100元', 'W04', 10000),
    ('申购R2理财两百五', 'W04', 25000),
    ('给房东转20', 'T01', 2000),
])
def test_amount_does_not_consume_date_phone_or_product_numbers(text, capability, amount):
    parsed = IntentParser().parse(text)
    assert parsed.capability_id == capability
    assert parsed.slots['amount_minor'] == amount


@pytest.mark.parametrize('text', ['每月1日给房东转账', '从2026-11-01起每月给房东转账', '申购R1'])
def test_non_monetary_numbers_do_not_supply_missing_amount(text):
    parsed = IntentParser().parse(text)
    assert 'amount_minor' not in parsed.slots
    assert parsed.missing


def make_agent(tmp_path: Path, planner):
    db = Database(f"sqlite:///{tmp_path / 'functional.db'}")
    load_seed(db)

    def transport(config, role, payload):
        if role == 'planner':
            return planner(payload)
        if role == 'fraud':
            return {'category': 'safe', 'confidence': 0.99}
        return {'message': '结果已更新，请查看明细。', 'fragments': payload.get('facts', [])}

    gateway = LLMGateway(db, ProviderConfig('primary', 'https://mock.invalid', 'secret', 'model'),
                         None, transport=transport)
    return AgentOrchestrator(db, llm=gateway)


def frame(capability, slots=None, missing=None, ambiguities=None):
    return {'intent': 'test', 'capability_id': capability, 'slots': slots or {},
            'missing': missing or [], 'ambiguities': ambiguities or [], 'suggested_next_action': 'clarify'}


def test_schedule_followup_preserves_payee_amount_and_normalizes_date(tmp_path):
    calls = 0
    def planner(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return frame('T04', {'recipient_name': '房东', 'amount_minor': 1000, 'period': 'monthly'}, ['next_due'])
        return {**frame('T04', {'recipient_name': '房东', 'amount_minor': 1000,
                                'period': 'monthly', 'next_due': '2026-11-01'}),
                'references': [{'slot': 'next_due', 'source': 'user', 'text': '2026年11月1日'}]}
    agent = make_agent(tmp_path, planner)
    first = agent.handle('user_001', 'schedule', '每月给房东10元')
    assert first.kind == 'clarification'
    assert 'next_due' not in first.message
    second = agent.handle('user_001', 'schedule', '从2026年11月1日开始，每月1日执行')
    assert second.kind == 'confirmation', second.model_dump()
    assert second.data['payload']['amount_minor'] == 1000
    assert second.data['payload']['recipient_name'] == '房东'
    assert second.data['payload']['next_due'] == '2026-11-01'
    assert any(row['label'] == '金额' and row['value'] == '¥10.00' for row in second.data['display'])
    result = agent.confirm('user_001', second.record_id, second.display_hash, second.data['receipt_id'])
    assert result.data['next_due'] == '2026-11-01'


def test_remove_card_restriction_cannot_be_downgraded_to_add(tmp_path):
    agent = make_agent(tmp_path, lambda _: frame('C04', {'card_id': 'card_001', 'restriction': 'online'}))
    service = CapabilityService(agent.database)
    service.execute('user_001', 'C03', {'card_id': 'card_001', 'restriction': 'online'}, 'restriction-setup')
    assert service.card_payment('user_001', 'card_001', 100) == 'CARD_RESTRICTED'
    prepared = agent.handle('user_001', 'remove', '我要解除银行卡的网购限制，不是限制网购')
    assert prepared.capability_id == 'C04'
    assert prepared.kind == 'confirmation'
    assert prepared.data['assurance'] == 'mfa'
    device = DeviceService(agent.database)
    token = device.register('user_001')
    agent.capabilities.device_tokens['user_001'] = token
    waiting = agent.confirm('user_001', prepared.record_id, prepared.display_hash, prepared.data['receipt_id'])
    assert waiting.kind == 'mfa'
    done = agent.verify('user_001', prepared.record_id, device.inbox(token)[0]['code'])
    assert done.kind == 'result'
    assert service.card_payment('user_001', 'card_001', 100) == 'approved'


def test_default_spending_categories_exclude_salary(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'analysis.db'}")
    load_seed(db)
    result = CapabilityService(db).execute('user_001', 'B01', {}, 'spending')
    assert 'txn_salary' not in {txn for row in result.data['rows'] for txn in row['txn_ids']}


def test_model_generated_spending_dsl_cannot_include_salary(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'dsl-analysis.db'}")
    load_seed(db)
    result = CapabilityService(db).execute('user_001', 'B01',
        {'dsl': {'source': 'transactions', 'group_by': ['category'], 'limit': 100}}, 'spending-dsl')
    assert 'txn_salary' not in {txn for row in result.data['rows'] for txn in row['txn_ids']}


def test_phone_reference_is_restored_locally_without_sending_phone_to_provider(tmp_path):
    calls = []
    def planner(payload):
        calls.append(json.dumps(payload, ensure_ascii=False))
        assert 'phone_ref_1' in payload['text']
        return frame('T02', {'phone': 'phone_ref_1', 'amount_minor': 2000})
    agent = make_agent(tmp_path, planner)
    result = agent.handle('user_001', 'private-phone', '给13800000001转20元')
    assert result.kind == 'confirmation'
    assert '13800000001' not in ''.join(calls)
    assert 'phone_ref_1' in ''.join(calls)
    assert result.data['payload']['phone'] == '13800000001'


def test_model_can_resolve_semantic_alias_using_owned_contact_names(tmp_path):
    contexts = []
    def planner(payload):
        contexts.append(payload['context'])
        return frame('T03', {'alias': '房东', 'amount_minor': 1000})
    agent = make_agent(tmp_path, planner)
    result = agent.handle('user_001', 'rent', '给房租转10元')
    assert {'recipient_name': '房东'} in contexts[0]['payees']
    assert result.kind == 'confirmation'
    assert result.data['payload']['alias'] == '房东'


def test_monthly_spending_does_not_require_user_to_write_dsl_or_report_slots(tmp_path):
    def planner(_):
        return {**frame('B01', {'start_month': '2026-10', 'end_month': '2026-10'}),
                'references': [{'slot': 'start_month', 'source': 'user', 'text': '2026年10月'},
                               {'slot': 'end_month', 'source': 'user', 'text': '2026年10月'}]}
    agent = make_agent(tmp_path, planner)
    result = agent.handle('user_001', 'analysis', '按类别统计2026年10月的消费金额')
    assert result.kind == 'facts'
    assert result.capability_id == 'B01'
    ids = {txn for row in result.data['rows'] for txn in row['txn_ids']}
    with agent.database.sessions() as session:
        expected = {row.id for row in session.query(Transaction).all()
                    if row.user_id == 'user_001' and row.direction == 'debit' and row.posted_at.startswith('2026-10')}
    assert ids == expected
    assert 'dsl' not in result.message and '待核实' not in result.message


def test_explicit_birthday_combination_cannot_be_reduced_to_one_order(tmp_path):
    agent = make_agent(tmp_path, lambda _: {**frame(None), 'intent': 'birthday_plan', 'plan_template': 'birthday',
                                          'slots': {'event_id': 'event_birthday', 'reserve_minor': 100000}})
    prepared = agent.handle('user_001', 'birthday',
                            '我爱人生日，预留1000元，生日前两天订鲜花和蛋糕')
    assert prepared.kind == 'plan_confirmation'
    assert prepared.data['plan_type'] == 'birthday'
    confirmed = agent.confirm('user_001', prepared.record_id, prepared.display_hash, prepared.data['receipt_id'])
    assert confirmed.kind == 'result'
    with agent.database.sessions() as session:
        orders = session.query(MerchantOrder).all()
        assert {order.item for order in orders} == {'flower', 'cake'}
        assert {order.delivery_date for order in orders} == {'2026-11-15'}


def test_pending_birthday_plan_survives_model_switch_to_reservation(tmp_path):
    count = 0
    def planner(_):
        nonlocal count
        count += 1
        if count == 1:
            return {**frame(None), 'intent': 'birthday_plan', 'plan_template': 'birthday',
                    'slots': {'event_id': 'event_birthday'}, 'missing': ['reserve_minor']}
        return frame('X02', {'amount_minor': 100000})
    agent = make_agent(tmp_path, planner)
    first = agent.handle('user_001', 'birthday', '为爱人安排生日鲜花和蛋糕')
    assert first.kind == 'clarification'
    second = agent.handle('user_001', 'birthday', '预留1000元，生日事件是event_birthday')
    assert second.kind == 'plan_confirmation'
    assert second.data['plan_type'] == 'birthday'


@pytest.mark.parametrize('item', ['flower', 'cake'])
def test_single_birthday_item_does_not_expand_to_both_items(tmp_path, item):
    def planner(_):
        return {**frame(None), 'intent': 'birthday_plan', 'plan_template': 'birthday',
                'slots': {'event_id': 'event_birthday', 'reserve_minor': 100000, 'items': [item]}}
    agent = make_agent(tmp_path, planner)
    prepared = agent.handle('user_001', 'single', f'生日预留1000元，只订{"鲜花" if item == "flower" else "蛋糕"}')
    assert prepared.kind == 'plan_confirmation'
    agent.confirm('user_001', prepared.record_id, prepared.display_hash, prepared.data['receipt_id'])
    with agent.database.sessions() as session:
        assert {order.item for order in session.query(MerchantOrder).all()} == {item}


def test_model_cannot_use_date_digits_as_monetary_source(tmp_path):
    def planner(_):
        return {**frame('T04', {'recipient_name': '房东', 'amount_minor': 100, 'next_due': '2026-11-01'}),
                'references': [{'slot': 'amount_minor', 'source': 'user', 'text': '1'}]}
    agent = make_agent(tmp_path, planner)
    result = agent.handle('user_001', 'date-money', '每月1日给房东转10元，从2026-11-01开始')
    assert result.kind == 'clarification'
    assert result.record_id is None


@pytest.mark.parametrize(('period', 'start', 'next_due'), [
    ('monthly', '2027-01-31', '2027-02-28'),
    ('weekly', '2027-01-31', '2027-02-07'),
    ('daily', '2027-01-31', '2027-02-01'),
    ('yearly', '2028-02-29', '2029-02-28'),
])
def test_recurring_schedule_uses_calendar_period_not_thirty_days(tmp_path, period, start, next_due):
    db = Database(f"sqlite:///{tmp_path / 'calendar.db'}")
    load_seed(db)
    clock = BusinessClock(datetime.fromisoformat(start).replace(tzinfo=timezone.utc))
    service = CapabilityService(db, clock=clock)
    prepared = service.execute('user_001', 'T04', {'recipient_name': '房东', 'amount_minor': 100,
                                                  'next_due': start, 'period': period, 'max_uses': 3}, 'calendar')
    service.dispatch_due('user_001')
    assert service.get('user_001', prepared.record_id).data['next_due'] == next_due


def test_generic_confirmation_display_is_bound_to_the_authorization(tmp_path):
    agent = make_agent(tmp_path, lambda _: frame('C04', {'card_id': 'card_001', 'restriction': 'online'}))
    prepared = agent.handle('user_001', 'display', '解除网购限制')
    with agent.database.atomic() as session:
        record = session.get(CapabilityRecord, prepared.record_id)
        data = json.loads(record.data_json)
        data['display'][0]['value'] = '设置限制'
        record.data_json = json.dumps(data)
    with pytest.raises(DomainError, match='HASH_MISMATCH'):
        agent.confirm('user_001', prepared.record_id, prepared.display_hash, prepared.data['receipt_id'])
