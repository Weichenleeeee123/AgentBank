import pytest
from sqlalchemy import select
from agentbank.auth.devices import DeviceService
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Card, Database, CapabilityRecord
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


def test_pin_changes_only_after_otp_and_survives_service_restart(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'pin.db'}")
    load_seed(db)
    s = CapabilityService(db, require_device_mfa=True, enforce_prepare=True)
    devices = DeviceService(db)
    token = devices.register('user_001')
    s.device_tokens['user_001'] = token
    for pin in ['4826', '7152']:
        r = s.execute('user_001', 'C08', {'card_id': 'card_001', 'new_pin': pin}, 'pin-' + pin)
        with db.sessions() as session:
            before = getattr(session.get(Card, 'card_001'), 'pin_hash', None)
        d = r.data
        s.confirm_capability('user_001', r.record_id, d['receipt_id'], d['action_hash'], d['display_hash'])
        with db.sessions() as session:
            assert getattr(session.get(Card, 'card_001'), 'pin_hash', None) == before
        s.verify_mfa('user_001', r.record_id, devices.inbox(token)[0]['code'])
        restarted = CapabilityService(db)
        assert restarted.verify_card_pin('user_001', 'card_001', pin)
        assert s.execute('user_001', 'C08', {'card_id': 'card_001', 'new_pin': pin}, 'pin-' + pin).status == 'succeeded'
        with pytest.raises(DomainError, match='IDEMPOTENCY_CONFLICT'):
            s.execute('user_001', 'C08', {'card_id': 'card_001', 'new_pin': '6666'}, 'pin-' + pin)
        assert not restarted.verify_card_pin('user_001', 'card_001', '9999')
        with db.sessions() as session:
            assert session.get(Card, 'card_001').pin_hash != before
            assert all(pin not in row.data_json for row in session.scalars(select(CapabilityRecord)))
    assert not restarted.verify_card_pin('user_001', 'card_001', '4826')
    with pytest.raises(DomainError, match='NOT_OWNER'):
        restarted.verify_card_pin('user_002', 'card_001', '7152')


@pytest.mark.parametrize('pin', ['', 'abc123', '123', '123456789', '[REDACTED]'])
def test_invalid_pin_never_prepares(tmp_path, pin):
    db = Database(f"sqlite:///{tmp_path / 'pin.db'}")
    load_seed(db)
    with pytest.raises(DomainError, match='PIN_POLICY_INVALID'):
        CapabilityService(db, enforce_prepare=True).execute('user_001', 'C08',
            {'card_id': 'card_001', 'new_pin': pin}, 'invalid')
