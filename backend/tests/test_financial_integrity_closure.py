"""Cross-path acceptance: real authorization, cash, ledger, history and reporting."""
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import select

from agentbank.analysis.insights import monthly_report, yearly_report
from agentbank.auth.devices import DeviceService
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.bank.service import TransferService
from agentbank.clock import utc_now_naive
from agentbank.db import (
    Account, Database, DailyRiskBucket, InvestmentOrder, LedgerEntry, Mandate,
    MerchantCatalog, Operation, Position, Reservation, Transaction,
)
from agentbank.errors import DomainError
from agentbank.security.breaker import CircuitBreaker
from agentbank.security.fraud import record_risk
from agentbank.seed.loader import load_seed


@pytest.fixture()
def bank(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'bank.db'}")
    load_seed(db)
    service = CapabilityService(db, require_device_mfa=True, enforce_prepare=True)
    devices = DeviceService(db)
    for user in ['user_001', 'user_002']:
        service.device_tokens[user] = devices.register(user)
    return service, devices


def perform(bank, cap, payload, user='user_001'):
    service, devices = bank
    result = service.execute(user, cap, payload, uuid.uuid4().hex)
    if result.status == 'awaiting_confirmation':
        d = result.data
        result = service.confirm_capability(user, result.record_id, d['receipt_id'], d['action_hash'], d['display_hash'])
    if result.status == 'awaiting_mfa':
        result = service.verify_mfa(user, result.record_id, devices.inbox(service.device_tokens[user])[0]['code'])
    return result


def assert_cash(service, expected, user='user_001'):
    a = service._account(user)
    assert (a.available_minor, a.ledger_balance_minor) == (expected, expected)


def change_state(service, state):
    if state in {'high', 'medium', 'unknown'}:
        record_risk(service.database, 'user_001', state)
    elif state == 'locked':
        for _ in range(3):
            CircuitBreaker(service.database).record_block('user_001', 'injection')
    else:
        with service.database.sessions.begin() as session:
            if state == 'frozen':
                session.get(Account, 'acc_001').status = 'frozen'
            elif state == 'cumulative':
                session.add(DailyRiskBucket(user_id='user_001', business_date=service.clock.business_date(), completed_minor=100000, pending_minor=0))


@pytest.mark.parametrize('cap', ['T04', 'X03', 'X04'])
@pytest.mark.parametrize('state', ['high', 'medium', 'unknown', 'locked', 'frozen', 'cumulative'])
def test_deferred_payment_rechecks_state_without_consuming_authority(bank, cap, state):
    s, _ = bank
    payload = ({'recipient_id': 'payee_landlord', 'amount_minor': 2000, 'period': 'once', 'max_uses': 1,
                'next_due': s.clock.business_date()} if cap == 'T04' else
               {'amount_minor': 5000, 'delivery_date': s.clock.business_date(), 'dispatch_on': s.clock.business_date()})
    perform(bank, cap, payload)
    change_state(s, state)
    changed = s.dispatch_due('user_001')
    assert changed and changed[0].status == 'paused'
    assert_cash(s, 500000)
    with s.database.sessions() as session:
        assert session.query(Operation).count() == session.query(LedgerEntry).count() == 0
        assert all(m.used_count == 0 for m in session.scalars(select(Mandate)))
        assert session.get(MerchantCatalog, 'flower').stock == 100


def test_scheduled_cash_history_and_report_are_consistent(bank):
    s, _ = bank
    perform(bank, 'T04', {'recipient_id': 'payee_landlord', 'amount_minor': 2000, 'period': 'once', 'max_uses': 1, 'next_due': s.clock.business_date()})
    assert s.dispatch_due('user_001')[0].status == 'succeeded'
    assert s.dispatch_due('user_001') == []
    assert_cash(s, 498000)
    rows = monthly_report(s.database, 'user_001', s.clock.business_date()[:7], s.clock.business_date()[:7])
    assert rows[0]['transfer_minor'] == 2000


def test_transfer_visible_in_history_and_month_report(bank):
    s, _ = bank
    t = TransferService(s.database)
    p = t.prepare('user_001', '房东', 2000)
    assert t.confirm('user_001', p.plan_id, p.receipt_id, p.display_hash).status == 'succeeded'
    rows = s.execute('user_001', 'Q02', {'start_date': s.clock.business_date()}, 'history').data['transactions']
    assert len(rows) == 1 and rows[0]['amount_minor'] == 2000
    assert monthly_report(s.database, 'user_001', s.clock.business_date()[:7], s.clock.business_date()[:7])[0]['transfer_minor'] == 2000


def test_subscription_charge_balances_ledger_and_is_once_per_due_date(bank):
    s, _ = bank
    first = s.simulate_subscription_charge('user_001', 'sub_video')
    second = s.simulate_subscription_charge('user_001', 'sub_video')
    assert first['charged'] and first['transaction_id'] == second['transaction_id']
    assert_cash(s, 497010)
    with s.database.sessions() as session:
        entries = session.scalars(select(LedgerEntry)).all()
        assert len(entries) == 2 and sum(e.amount_minor for e in entries) == 0


@pytest.mark.parametrize('state', ['frozen', 'locked', 'high'])
def test_subscription_charge_rejects_unsafe_account(bank, state):
    s, _ = bank
    change_state(s, state)
    result = s.simulate_subscription_charge('user_001', 'sub_video')
    assert not result['charged']
    assert_cash(s, 500000)


def test_merchant_spend_updates_shared_daily_bucket(bank):
    s, _ = bank
    perform(bank, 'X03', {'amount_minor': 5000, 'delivery_date': s.clock.business_date()})
    assert s.dispatch_due('user_001')[0].status == 'succeeded'
    with s.database.sessions() as session:
        assert session.get(DailyRiskBucket, ('user_001', s.clock.business_date())).completed_minor == 5000
    t = TransferService(s.database)
    assert t.prepare('user_001', '房东', 96000).tier_display == 'R'


def test_frozen_account_cannot_subscribe_even_with_otp(bank):
    s, _ = bank
    perform(bank, 'W03', {'answers': [2] * 10})
    change_state(s, 'frozen')
    with pytest.raises(DomainError, match='ACCOUNT_NOT_ACTIVE'):
        perform(bank, 'W04', {'product_id': 'product_R1', 'amount_minor': 1000})
    assert_cash(s, 500000)


def test_frozen_account_cannot_reserve_birthday_funds_after_confirmation(bank):
    s, _ = bank
    result = s.execute('user_001', 'X02', {'amount_minor': 10000, 'plan_id': 'frozen-birthday'}, 'frozen-reserve')
    assert result.status == 'awaiting_confirmation'
    data = result.data
    change_state(s, 'frozen')
    with pytest.raises(DomainError, match='ACCOUNT_NOT_ACTIVE'):
        s.confirm_capability('user_001', result.record_id, data['receipt_id'],
                             data['action_hash'], data['display_hash'])
    assert_cash(s, 500000)
    with s.database.sessions() as session:
        assert session.scalar(select(Reservation)) is None


def test_inverse_order_redemptions_leave_no_phantom_assets(bank):
    s, _ = bank
    perform(bank, 'W03', {'answers': [2] * 10})
    a = perform(bank, 'W04', {'product_id': 'product_R1', 'amount_minor': 1000})
    b = perform(bank, 'W04', {'product_id': 'product_R1', 'amount_minor': 2000})
    for result, expected_shares in [(b, 10), (a, 0)]:
        perform(bank, 'W05', {'order_id': result.data['order_id']})
        s.clock.advance(days=3)
        s.progress_investments('user_001', due_only=True)
        with s.database.sessions() as session:
            assert sum(p.shares for p in session.scalars(select(Position))) == expected_shares
    assert_cash(s, 500000)
    assert s.progress_investments('user_001', due_only=True) == []
    positions = s.execute('user_001', 'W06', {}, 'positions').data['positions']
    assert all(p['market_value_minor'] == p['pnl_minor'] == 0 for p in positions)


def test_redemption_uses_business_clock_and_product_delay(bank):
    s, _ = bank
    perform(bank, 'W03', {'answers': [2] * 10})
    s.clock.advance(days=30)
    bought = perform(bank, 'W04', {'product_id': 'product_R1', 'amount_minor': 1000})
    perform(bank, 'W05', {'order_id': bought.data['order_id']})
    assert s.progress_investments('user_001', due_only=True) == []
    with s.database.sessions() as session:
        order = session.get(InvestmentOrder, bought.data['order_id'])
        assert order.expected_settlement_at == (s.clock.now() + timedelta(days=1)).replace(tzinfo=None)


def test_subyuan_investment_cannot_create_zero_share_position(bank):
    perform(bank, 'W03', {'answers': [2] * 10})
    with pytest.raises(DomainError, match='INVESTMENT_AMOUNT_INCREMENT'):
        perform(bank, 'W04', {'product_id': 'product_R1', 'amount_minor': 1})
    assert_cash(bank[0], 500000)


def test_third_transfer_in_ten_minutes_requires_mfa(bank):
    s, _ = bank
    t = TransferService(s.database)
    for _ in range(2):
        p = t.prepare('user_001', '房东', 1000)
        assert t.confirm('user_001', p.plan_id, p.receipt_id, p.display_hash).status == 'succeeded'
    p = t.prepare('user_001', '房东', 1000)
    assert p.tier_display == 'R'


def test_missing_period_has_no_percentage_change(bank):
    s, _ = bank
    row = monthly_report(s.database, 'user_001', '2026-11', '2026-11')[0]
    assert row['missing'] and row['mom_change'] is None and row['yoy_change'] is None
    year = yearly_report(s.database, 'user_001', 2027, 2027)[0]
    assert year['yoy_change'] is None


def test_aa_receipt_records_both_users_histories(bank):
    s, _ = bank
    bill = perform(bank, 'T05', {'total_minor': 10000, 'participant_ids': ['user_001', 'user_002']})
    perform(bank, 'T05', {'action': 'pay_share', 'bill_id': bill.data['bill_id']}, 'user_002')
    with s.database.sessions() as session:
        histories = session.scalars(select(Transaction).where(Transaction.posted_at >= s.clock.business_date())).all()
        assert {t.user_id for t in histories} == {'user_001', 'user_002'}
        assert all(t.direction == 'transfer' for t in histories)


def test_failed_merchant_payment_preserves_reservation(bank):
    s, _ = bank
    reserved = perform(bank, 'X02', {'amount_minor': 10000, 'plan_id': 'reserve-test'})
    perform(bank, 'X03', {'delivery_date': s.clock.business_date(), 'amount_minor': 5000,
                          'reservation_id': reserved.data['reservation_id']})
    change_state(s, 'frozen')
    assert s.dispatch_due('user_001')[0].status == 'paused'
    with s.database.sessions() as session:
        r = session.get(Reservation, reserved.data['reservation_id'])
        assert r.amount_minor == 10000 and r.status == 'active'
        assert session.get(Account, 'acc_001').available_minor == 490000


def test_frequency_window_uses_real_time_not_business_fast_forward(bank):
    s, _ = bank
    t = TransferService(s.database)
    for _ in range(2):
        p = t.prepare('user_001', '房东', 1000)
        t.confirm('user_001', p.plan_id, p.receipt_id, p.display_hash)
    with s.database.sessions.begin() as session:
        for operation in session.scalars(select(Operation)):
            operation.created_at = utc_now_naive() - timedelta(minutes=11)
    assert t.prepare('user_001', '房东', 1000).tier_display == 'Y'


@pytest.mark.parametrize('state', ['high', 'medium', 'unknown', 'locked'])
def test_investment_commit_rechecks_security_state(bank, state):
    s, devices = bank
    perform(bank, 'W03', {'answers': [2] * 10})
    r = s.execute('user_001', 'W04', {'product_id': 'product_R1', 'amount_minor': 1000}, 'risk-invest')
    d = r.data
    s.confirm_capability('user_001', r.record_id, d['receipt_id'], d['action_hash'], d['display_hash'])
    change_state(s, state)
    with pytest.raises(DomainError):
        s.verify_mfa('user_001', r.record_id, devices.inbox(s.device_tokens['user_001'])[0]['code'])
    assert_cash(s, 500000)


def test_fast_forward_keeps_transfer_and_deferred_daily_totals_together(bank):
    s, _ = bank
    s.clock.advance(days=1)
    perform(bank, 'T04', {'recipient_id': 'payee_landlord', 'amount_minor': 60000,
        'period': 'once', 'max_uses': 1, 'next_due': s.clock.business_date()})
    assert s.dispatch_due('user_001')[0].status == 'succeeded'
    assert TransferService(s.database).prepare('user_001', '房东', 60000).tier_display == 'R'


def test_partial_year_does_not_compare_against_full_year(bank):
    s, _ = bank
    row = yearly_report(s.database, 'user_001', 2026, 2026)[0]
    assert row['yoy_change'] is None
    assert row['missing_months'] == [11, 12]


def test_frozen_redemption_defers_until_account_active(bank):
    s, _ = bank
    perform(bank, 'W03', {'answers': [2] * 10})
    buy = perform(bank, 'W04', {'product_id': 'product_R1', 'amount_minor': 1000})
    perform(bank, 'W05', {'order_id': buy.data['order_id']})
    change_state(s, 'frozen')
    s.clock.advance(days=2)
    assert s.progress_investments('user_001', due_only=True) == []
    assert_cash(s, 499000)
    with s.database.sessions.begin() as session:
        session.get(Account, 'acc_001').status = 'active'
    assert len(s.progress_investments('user_001', due_only=True)) == 1
    assert_cash(s, 500000)


def test_ambiguous_legacy_lot_does_not_block_other_settlement(bank):
    s, _ = bank
    perform(bank, 'W03', {'answers': [2] * 10})
    buys = [perform(bank, 'W04', {'product_id': 'product_R1', 'amount_minor': amount})
            for amount in [1000, 1000, 2000]]
    for buy in buys:
        perform(bank, 'W05', {'order_id': buy.data['order_id']})
    with s.database.sessions.begin() as session:
        for buy in buys[:2]:
            session.get(InvestmentOrder, buy.data['order_id']).position_id = None
    s.clock.advance(days=3)
    results = s.progress_investments('user_001', due_only=True)
    assert any(r.status == 'succeeded' and r.data['order_id'] == buys[2].data['order_id'] for r in results)
    assert_cash(s, 498000)
    with s.database.sessions() as session:
        assert all(session.get(InvestmentOrder, b.data['order_id']).status == 'processing' for b in buys[:2])
