from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentbank.api.app import create_app
from agentbank.audit.store import PersistentAudit
from agentbank.db import Database
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.security.dlp import redact_payload
from agentbank.security.rate_limit import SlidingWindowLimiter


def test_audit_checkpoint_detects_tail_truncation(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'audit-gate.db'}")
    load_seed(database)
    audit = PersistentAudit(database)
    with database.sessions.begin() as session:
        audit.append(session, "trace-1", "user_001", "test", {"value": "ok"})
    audit.checkpoint()
    assert audit.verify() is True
    with database.sessions.begin() as session:
        from agentbank.db import AuditLog
        session.query(AuditLog).delete()
    assert audit.verify() is False


def test_pairing_code_is_one_time_and_dlp_redacts_sensitive_values(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'pairing.db'}")
    load_seed(database)
    from agentbank.auth.devices import DeviceService
    service = DeviceService(database)
    code = service.create_pairing_code("user_001")
    token = service.redeem_pairing_code(code)
    assert service.user_for_token(token) == "user_001"
    with pytest.raises(DomainError, match="PAIRING_CODE_INVALID"):
        service.redeem_pairing_code(code)
    redacted = redact_payload({"card": "6222021234567890", "otp": "验证码 123456"})
    assert "6222021234567890" not in str(redacted)
    assert "123456" not in str(redacted)


def test_rate_limit_and_demo_run_endpoint(tmp_path: Path, monkeypatch) -> None:
    limiter = SlidingWindowLimiter(limit=1)
    limiter.check("user")
    with pytest.raises(DomainError, match="RATE_LIMITED"):
        limiter.check("user")
    monkeypatch.setenv("AGENTBANK_ADMIN_TOKEN", "admin-test")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'demo.db'}"))
    response = client.post("/admin/v1/self-test", headers={"X-Admin-Token": "admin-test"})
    assert response.status_code == 200
    assert response.json()["demo_run_id"]
