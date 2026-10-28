from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import CardApplication, Database
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


def make_service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'c.db'}")
    load_seed(database)
    return CapabilityService(database)


def test_c01_application_virtual_and_physical_lifecycle(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    virtual = service.execute("user_001", "C01", {"virtual": True}, "c01-virtual")
    assert virtual.data["status"] == "active"
    physical = service.execute("user_001", "C01", {"virtual": False, "assurance": "mfa"}, "c01-physical")
    assert physical.data["status"] == "pending"
    with service.database.sessions() as session:
        assert session.query(CardApplication).filter(CardApplication.card_id == physical.data["card_id"]).one().status == "pending"
    progressed = service.advance_business_state("user_001")
    assert any(item.record_id == physical.record_id and item.data["status"] == "active" for item in progressed)


def test_c02_limits_and_c03_c04_restrictions_affect_payment(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    lowered = service.execute("user_001", "C02", {"card_id": "card_001", "limit_minor": 90_000}, "c02-down")
    assert lowered.data["limit_minor"] == 90_000
    with pytest.raises(DomainError, match="MFA_REQUIRED"):
        service.execute("user_001", "C02", {"card_id": "card_001", "limit_minor": 110_000}, "c02-up-deny")
    service.execute("user_001", "C02", {"card_id": "card_001", "limit_minor": 110_000, "assurance": "mfa"}, "c02-up")
    service.execute("user_001", "C03", {"card_id": "card_001", "restriction": "online"}, "c03-add")
    assert service.card_payment("user_001", "card_001", 100) == "CARD_RESTRICTED"
    service.execute("user_001", "C04", {"card_id": "card_001", "restriction": "online", "assurance": "mfa"}, "c04-remove")
    assert service.card_payment("user_001", "card_001", 100) == "approved"


def test_c05_c06_c07_state_guards_and_c08_secret_not_persisted(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(DomainError, match="INVALID_CARD_STATE"):
        service.execute("user_001", "C06", {"card_id": "card_001", "assurance": "mfa"}, "c06-before-loss")
    service.execute("user_001", "C05", {"card_id": "card_001", "assurance": "mfa"}, "c05-loss")
    assert service.card_payment("user_001", "card_001", 100) == "CARD_RESTRICTED"
    service.execute("user_001", "C06", {"card_id": "card_001", "assurance": "mfa"}, "c06-release")
    service.execute("user_001", "C07", {"card_id": "card_001"}, "c07-freeze")
    assert service.card_payment("user_001", "card_001", 100) == "CARD_RESTRICTED"
    with pytest.raises(DomainError, match="MFA_REQUIRED"):
        service.execute("user_001", "C07", {"card_id": "card_001", "action": "unfreeze"}, "c07-unfreeze-missing")
    pin = service.execute("user_001", "C08", {"card_id": "card_001", "new_pin": "1234", "assurance": "mfa"}, "c08-pin")
    assert "1234" not in str(pin.data)
