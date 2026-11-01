from pathlib import Path

import pytest
from sqlalchemy import select

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Account, AAShare, Card, Database, DailyRiskBucket, LedgerEntry
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


def service(tmp_path: Path, *, device: bool = True) -> CapabilityService:
    db = Database(f"sqlite:///{tmp_path / 'review.db'}")
    load_seed(db)
    return CapabilityService(db, require_device_mfa=device, enforce_prepare=True)


def confirm(s, user, record):
    d = record.data
    return s.confirm_capability(user, record.record_id, d['receipt_id'], d['action_hash'], d['display_hash'])


def bill(s, total=400000):
    return confirm(s, 'user_001', s.execute('user_001', 'T05', {
        'total_minor': total, 'participant_ids': ['user_001', 'user_002']}, 'bill')).data['bill_id']


def test_stale_limit_decrease_cannot_raise_without_mfa(tmp_path):
    s = service(tmp_path)
    old = s.execute('user_001', 'C02', {'card_id': 'card_001', 'limit_minor': 90000}, 'old')
    confirm(s, 'user_001', s.execute('user_001', 'C02', {'card_id': 'card_001', 'limit_minor': 50000}, 'lower'))
    with pytest.raises(DomainError, match='MFA_REQUIRED'):
        confirm(s, 'user_001', old)
    with s.database.sessions() as session:
        assert session.get(Card, 'card_001').limit_minor == 50000


def test_aa_binds_actual_payment_and_requires_mfa(tmp_path):
    s = service(tmp_path)
    prepared = s.execute('user_002', 'T05', {'action': 'pay_share', 'bill_id': bill(s)}, 'pay')
    assert prepared.data['assurance'] == 'mfa'
    assert prepared.data['payload']['amount_minor'] == 200000
    assert prepared.data['payload']['recipient_user_id'] == 'user_001'
    assert any(row['label'] == '金额' and '2000.00' in row['value'] for row in prepared.data['display'])
    assert any(row['label'] == '收款人' for row in prepared.data['display'])
    assert confirm(s, 'user_002', prepared).status == 'awaiting_mfa'
    with s.database.sessions() as session:
        assert session.scalar(select(Account).where(Account.user_id == 'user_002')).available_minor == 500000


def test_aa_settlement_credits_real_owner_once_and_counts_daily_total(tmp_path):
    s = service(tmp_path, device=False)
    prepared = s.execute('user_002', 'T05', {'action': 'pay_share', 'bill_id': bill(s)}, 'pay')
    result = confirm(s, 'user_002', prepared)
    with pytest.raises(DomainError):
        confirm(s, 'user_002', prepared)
    with s.database.sessions() as session:
        assert session.scalar(select(Account).where(Account.user_id == 'user_002')).available_minor == 300000
        owner = session.scalar(select(Account).where(Account.user_id == 'user_001'))
        assert owner.available_minor == owner.ledger_balance_minor == 700000
        entries = session.scalars(select(LedgerEntry).where(LedgerEntry.operation_id == result.data['operation_id'])).all()
        assert sum(e.amount_minor for e in entries) == 0
        assert {e.account_id for e in entries} <= set(session.scalars(select(Account.id)).all())
        assert session.get(DailyRiskBucket, ('user_002', s.clock.business_date())).completed_minor == 200000


def test_aa_stale_share_cannot_change_authorized_amount(tmp_path):
    s = service(tmp_path)
    bill_id = bill(s, 10000)
    old = s.execute('user_002', 'T05', {'action': 'pay_share', 'bill_id': bill_id}, 'pay')
    with s.database.sessions.begin() as session:
        session.scalar(select(AAShare).where(AAShare.bill_id == bill_id, AAShare.participant_id == 'user_002')).amount_minor = 200000
    with pytest.raises(DomainError, match='PAYMENT_SNAPSHOT_CHANGED'):
        confirm(s, 'user_002', old)


def test_aa_cumulative_upgrade_at_commit(tmp_path):
    s = service(tmp_path)
    old = s.execute('user_002', 'T05', {'action': 'pay_share', 'bill_id': bill(s, 100000)}, 'pay')
    assert old.data['assurance'] == 'confirm'
    with s.database.sessions.begin() as session:
        session.add(DailyRiskBucket(user_id='user_002', business_date=s.clock.business_date(), completed_minor=60000, pending_minor=0))
    with pytest.raises(DomainError, match='MFA_REQUIRED'):
        confirm(s, 'user_002', old)


def test_aa_high_value_completes_only_after_actual_device_otp(tmp_path):
    from agentbank.auth.devices import DeviceService
    s = service(tmp_path)
    device = DeviceService(s.database)
    token = device.register('user_002')
    s.device_tokens['user_002'] = token
    prepared = s.execute('user_002', 'T05', {'action': 'pay_share', 'bill_id': bill(s)}, 'pay')
    assert confirm(s, 'user_002', prepared).status == 'awaiting_mfa'
    result = s.verify_mfa('user_002', prepared.record_id, device.inbox(token)[0]['code'])
    assert result.status == 'succeeded'
    with s.database.sessions() as session:
        assert session.scalar(select(Account).where(Account.user_id == 'user_001')).available_minor == 700000


@pytest.mark.parametrize('share_minor,completed,expected', [(100000,0,'confirm'), (100001,0,'mfa'), (50000,50000,'confirm'), (50001,50000,'mfa')])
def test_aa_daily_boundary(tmp_path, share_minor, completed, expected):
    s = service(tmp_path)
    with s.database.sessions.begin() as session:
        session.add(DailyRiskBucket(user_id='user_002', business_date=s.clock.business_date(), completed_minor=completed, pending_minor=0))
    prepared = s.execute('user_002', 'T05', {'action': 'pay_share', 'bill_id': bill(s, share_minor * 2)}, 'pay')
    assert prepared.data['assurance'] == expected
