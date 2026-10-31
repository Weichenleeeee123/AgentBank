from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentbank.api.app import create_app
from agentbank.auth.devices import DeviceService
from agentbank.db import Database
from agentbank.errors import DomainError
from agentbank.security.cooling import CoolingPeriodService
from agentbank.security.dlp import redact_payload
from agentbank.security.fragments import ReplyFragmentChecker
from agentbank.audit.store import PersistentAudit
from agentbank.seed.loader import load_seed


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'm3-closure.db'}")
    load_seed(database)
    return database


def test_cooling_requires_action_and_revision_and_has_reconfirm_window(database: Database) -> None:
    service = CoolingPeriodService(database, duration=timedelta(minutes=30),
                                   reconfirm_window=timedelta(minutes=10))
    started = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)
    view = service.start("user_001", "plan-1", 1000, action_hash="sha256:a", plan_revision=2, now=started)
    with pytest.raises(DomainError, match="COOLING_PERIOD_ACTIVE"):
        service.release(view.id, action_hash="sha256:a", plan_revision=2,
                        now=started + timedelta(minutes=29), reconfirmed=True)
    with pytest.raises(DomainError, match="ACTION_CHANGED"):
        service.release(view.id, action_hash="sha256:b", plan_revision=2,
                        now=started + timedelta(minutes=35), reconfirmed=True)
    assert service.release(view.id, action_hash="sha256:a", plan_revision=2,
                           now=started + timedelta(minutes=35), reconfirmed=True) is True


def test_cooling_reconfirmation_window_expires(database: Database) -> None:
    service = CoolingPeriodService(database, duration=timedelta(minutes=30),
                                   reconfirm_window=timedelta(minutes=10))
    started = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)
    view = service.start("user_001", "plan-1", 1000, action_hash="sha256:a", plan_revision=2, now=started)
    with pytest.raises(DomainError, match="COOLING_RECONFIRMATION_REQUIRED"):
        service.release(view.id, action_hash="sha256:a", plan_revision=2,
                        now=started + timedelta(minutes=41), reconfirmed=True)


def test_otp_lockout_persists_across_sessions_and_binds_action(database: Database) -> None:
    service = DeviceService(database)
    service.register("user_001")
    code = service.issue_otp("user_001", "receipt-1", action_hash="sha256:a")
    assert code
    for _ in range(3):
        with pytest.raises(DomainError, match="OTP_INVALID"):
            service.verify("user_001", "000000", receipt_id="receipt-1", action_hash="sha256:a")
    assert service.is_user_locked("user_001") is True
    with pytest.raises(DomainError, match="OTP_LOCKED"):
        service.verify("user_001", code, receipt_id="receipt-1", action_hash="sha256:a")


def test_otp_rejects_cross_action_replay(database: Database) -> None:
    service = DeviceService(database)
    code = service.issue_otp("user_001", "receipt-1", action_hash="sha256:a")
    with pytest.raises(DomainError, match="OTP_ACTION_MISMATCH"):
        service.verify("user_001", code, receipt_id="receipt-1", action_hash="sha256:b")


def test_app_session_cannot_read_device_inbox_or_create_pairing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBANK_ADMIN_TOKEN", "admin-test")
    app = create_app(f"sqlite:///{tmp_path / 'device-boundary.db'}")
    client = TestClient(app)
    user_token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {user_token}"}
    assert client.get("/device/v1/inbox", headers=headers).status_code in {401, 403}
    assert client.post("/admin/v1/devices/pairing-code", headers=headers,
                       json={"user_id": "user_001"}).status_code in {401, 403}


def test_dlp_redacts_standalone_otp_in_nested_payload() -> None:
    redacted = redact_payload({"nested": ["OTP 123456", {"value": "验证码：654321"}]})
    assert "123456" not in str(redacted)
    assert "654321" not in str(redacted)


def test_dlp_redacts_otp_key_and_standalone_six_digits() -> None:
    redacted = redact_payload({"otp": 123456, "message": "备用验证码 777777", "plain": "888888"})
    assert redacted["otp"] == "[OTP_REDACTED]"
    assert "777777" not in str(redacted)
    assert "888888" not in str(redacted)


def test_reply_checker_rejects_numeric_and_status_claims_in_text() -> None:
    checker = ReplyFragmentChecker()
    for text in ("转账已经成功", "余额是 100 元", "结果待核实之外已完成"):
        with pytest.raises(DomainError):
            checker.validate([{"type": "text", "text": text}])


def test_audit_checkpoint_and_verify_creates_final_evidence(database: Database) -> None:
    audit = PersistentAudit(database)
    with database.sessions.begin() as session:
        audit.append(session, "trace-checkpoint", "user_001", "security.test", {"ok": True})
    checkpoint = audit.checkpoint_and_verify()
    assert checkpoint is not None
    assert audit.verify() is True
