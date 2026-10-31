import json

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.auth.devices import DeviceService
from agentbank.db import CapabilityRecord, Database, ReceiptRecord
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


def test_pin_display_is_bound_to_confirmation_and_owner(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'pin.db'}")
    load_seed(database)
    service = CapabilityService(database, enforce_prepare=True)
    prepared = service.execute('user_001', 'C08', {'card_id': 'card_001', 'new_pin': '4826'}, 'pin-display')
    with database.atomic() as session:
        record = session.get(CapabilityRecord, prepared.record_id)
        data = json.loads(record.data_json)
        data['display'][1]['value'] = 'card_foreign'
        record.data_json = json.dumps(data)
    with pytest.raises(DomainError, match='HASH_MISMATCH'):
        service.confirm_capability('user_001', prepared.record_id, prepared.data['receipt_id'],
                                   prepared.data['action_hash'], prepared.data['display_hash'])


def test_pin_cannot_report_success_for_foreign_card(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'pin-owner.db'}")
    load_seed(database)
    service = CapabilityService(database)
    with pytest.raises(DomainError, match='NOT_OWNER'):
        service.execute('user_002', 'C08', {'card_id': 'card_001', 'new_pin': '4826', 'assurance': 'mfa'}, 'pin-owner')


def test_pin_prepare_rejects_foreign_card_before_issuing_confirmation(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'pin-prepare-owner.db'}")
    load_seed(database)
    service = CapabilityService(database, enforce_prepare=True, require_device_mfa=True)
    with pytest.raises(DomainError, match='NOT_OWNER'):
        service.execute('user_002', 'C08', {'card_id': 'card_001', 'new_pin': '4826'}, 'foreign-prepared')


def test_pin_direct_mfa_pending_can_verify_without_storing_pin(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'pin-direct.db'}")
    load_seed(database)
    service = CapabilityService(database, require_device_mfa=True)
    prepared = service.execute('user_001', 'C08', {'card_id': 'card_001', 'new_pin': '4826',
                                                  'assurance': 'mfa'}, 'direct-pin')
    code = DeviceService(database).issue_otp('user_001', prepared.record_id, prepared.data['action_hash'])
    result = service.verify_mfa('user_001', prepared.record_id, code)
    assert result.status == 'succeeded'
    assert result.data['secret_persisted'] is False
    with database.sessions() as session:
        assert '4826' not in session.get(CapabilityRecord, prepared.record_id).data_json


def test_pin_confirmation_then_mfa_succeeds_and_consumes_receipt(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'pin-prepared.db'}")
    load_seed(database)
    service = CapabilityService(database, enforce_prepare=True, require_device_mfa=True)
    prepared = service.execute('user_001', 'C08', {'card_id': 'card_001', 'new_pin': '4826'}, 'prepared-pin')
    waiting = service.confirm_capability('user_001', prepared.record_id, prepared.data['receipt_id'],
                                         prepared.data['action_hash'], prepared.data['display_hash'])
    assert waiting.status == 'awaiting_mfa'
    code = DeviceService(database).issue_otp('user_001', prepared.data['receipt_id'], prepared.data['action_hash'])
    assert service.verify_mfa('user_001', prepared.record_id, code).data['status'] == 'pin_changed'
    with database.sessions() as session:
        assert session.get(ReceiptRecord, prepared.data['receipt_id']).status == 'consumed'


def test_pin_payload_is_revalidated_after_device_verification(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{tmp_path / 'pin-race.db'}")
    load_seed(database)
    service = CapabilityService(database, require_device_mfa=True)
    prepared = service.execute('user_001', 'C08', {'card_id': 'card_001', 'new_pin': '4826',
                                                  'assurance': 'mfa'}, 'racing-pin')
    code = DeviceService(database).issue_otp('user_001', prepared.record_id, prepared.data['action_hash'])
    original = DeviceService.verify

    def change_after_verification(device, *args, **kwargs):
        result = original(device, *args, **kwargs)
        with database.atomic() as session:
            row = session.get(CapabilityRecord, prepared.record_id)
            data = json.loads(row.data_json)
            data['display'][1]['value'] = 'different_card'
            row.data_json = json.dumps(data)
        return result

    monkeypatch.setattr(DeviceService, 'verify', change_after_verification)
    with pytest.raises(DomainError, match='HASH_MISMATCH'):
        service.verify_mfa('user_001', prepared.record_id, code)
    with database.sessions() as session:
        assert session.get(CapabilityRecord, prepared.record_id).status == 'awaiting_mfa'
