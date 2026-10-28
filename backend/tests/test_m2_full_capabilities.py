from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


@pytest.fixture()
def service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'full.db'}")
    load_seed(database)
    return CapabilityService(database)


@pytest.mark.parametrize("capability_id", [
    "Q01", "Q02", "T02", "T03", "T04", "T05", "B01", "B02", "B03", "B04",
    "W01", "W02", "W03", "W04", "W05", "W06", "C01", "C02", "C03", "C04",
    "C05", "C06", "C07", "C08", "S01", "S02", "S03", "X01", "X02", "X03", "X04", "X05",
])
def test_every_declared_capability_has_a_business_result(service: CapabilityService, capability_id: str) -> None:
    if capability_id == "W01":
        service.execute("user_001", "W03", {"answers": [1] * 10}, "setup-W03")
    if capability_id == "W04":
        service.execute("user_001", "W03", {"answers": [1] * 10}, "setup-W03-W04")
    if capability_id == "C06":
        service.execute("user_001", "C05", {"card_id": "card_001"}, "setup-C05-C06")
    result = service.execute("user_001", capability_id, {"amount_minor": 1_000, "recipient_name": "房东"}, f"cap-{capability_id}-1")
    assert result.capability_id == capability_id
    assert result.record_id
    assert result.status not in {"unimplemented", "stub"}
    assert result.evidence["audit_event"]


def test_capability_commands_are_idempotent_and_owner_bound(service: CapabilityService) -> None:
    first = service.execute("user_001", "X02", {"amount_minor": 10_000}, "same-key")
    second = service.execute("user_001", "X02", {"amount_minor": 10_000}, "same-key")
    assert first.record_id == second.record_id
    with pytest.raises(DomainError, match="NOT_OWNER"):
        service.get("user_002", first.record_id)


def test_generic_capability_api_returns_business_record(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from agentbank.api.app import create_app

    client = TestClient(create_app(f"sqlite:///{tmp_path / 'full-api.db'}"))
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    response = client.post("/v1/capabilities/Q01", headers={"Authorization": f"Bearer {token}"}, json={
        "capability_id": "Q01", "idempotency_key": "api-q01", "payload": {},
    })
    assert response.status_code == 200
    assert response.json()["data"]["as_of"]
    record = client.get(f"/v1/capability-records/{response.json()['record_id']}", headers={"Authorization": f"Bearer {token}"})
    assert record.status_code == 200
