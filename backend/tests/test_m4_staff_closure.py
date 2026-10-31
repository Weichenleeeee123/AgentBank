from datetime import timedelta
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from agentbank.api.staff import register_staff_routes
from agentbank.audit.store import PersistentAudit
from agentbank.clock import utc_now_naive
from agentbank.db import (AuditLog, Database, HandoffCase, LedgerEntry, LLMUsage,
                          ReceiptRecord, SecurityCounter, SecurityLock, UserNotification)
from agentbank.errors import DomainError
from agentbank.workflow.recovery import HandoffService


@pytest.fixture
def staff(monkeypatch):
    monkeypatch.setenv("AGENTBANK_STAFF_TOKEN", "staff-secret")
    db = Database("sqlite://")
    db.create_schema()
    app = FastAPI()
    register_staff_routes(app, db)
    case = HandoffService(db).open("owner", "transfer", "help")
    with db.sessions.begin() as session:
        row = session.get(HandoffCase, case.id)
        row.context_json = json.dumps({"owner_snapshot": "owner", "authorization": [{"id": "old"}]})
        session.add(LedgerEntry(id="ledger", operation_id="operation", account_id="account",
                                amount_minor=100, direction="debit"))
        session.add(ReceiptRecord(id="old", subject_id="owner", plan_id="plan", plan_revision=1,
                                 node_ids_json="[]", action_hash="action", display_hash="display",
                                 assurance="otp", issued_at=utc_now_naive(),
                                 expires_at=utc_now_naive() + timedelta(hours=1), status="authorized"))
    return db, app, TestClient(app), case.id


def test_staff_draft_and_retry_are_safe_intent_only(staff):
    db, app, client, case_id = staff
    headers = {"X-Staff-Token": "staff-secret"}
    base = f"/staff/v1/handoffs/{case_id}"
    response = client.post(base + "/draft", headers=headers,
                           json={"note": "电话13800138000", "user_message": "请重新确认验证码123456"})
    assert response.status_code == 200
    response = client.post(base + "/retry-request", headers=headers, json={"note": "交还用户"})
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_user"
    with db.sessions() as session:
        case = session.get(HandoffCase, case_id)
        context = json.loads(case.context_json)
        assert context["owner_snapshot"] == "owner"
        assert context["authorization"] == [{"id": "old"}]
        assert "13800138000" not in case.context_json
        assert "123456" not in case.context_json
        assert context["retry_request"]["requires_fresh_authorization"] is True
        assert context["retry_request"]["receipt_ids"] == []
        assert case.status == "open"
        assert session.get(ReceiptRecord, "old").status == "authorized"
        assert len(session.scalars(select(LedgerEntry)).all()) == 1
        notifications = session.scalars(select(UserNotification)).all()
        assert len(notifications) == 1 and notifications[0].user_id == "owner"
        assert "123456" not in notifications[0].body
        audit = session.scalars(select(AuditLog)).all()
        assert "123456" not in "".join(row.payload_json for row in audit)
    assert client.post(base + "/retry-request", headers=headers, json={"note": "again"}).status_code == 409
    assert PersistentAudit(db).verify()


def test_staff_permissions_validation_and_closed_state(staff, monkeypatch):
    db, app, client, case_id = staff
    base = f"/staff/v1/handoffs/{case_id}"
    for path in ["/staff/v1/metrics", base + "/draft", base + "/retry-request"]:
        response = client.get(path) if path.endswith("metrics") else client.post(path, json={"note": "x"})
        assert response.status_code == 403
    headers = {"X-Staff-Token": "staff-secret"}
    for payload in [{"note": "x", "amount_minor": 1}, {"note": 3}, {"note": "x" * 2001}]:
        assert client.post(base + "/draft", headers=headers, json=payload).status_code == 422
    HandoffService(db).resolve(case_id, "done")
    for action in ["draft", "retry-request"]:
        assert client.post(base + "/" + action, headers=headers, json={"note": "x"}).status_code == 409
    with pytest.raises(DomainError):
        HandoffService(db).resolve(case_id, "again")
    monkeypatch.delenv("AGENTBANK_STAFF_TOKEN")
    assert client.get("/staff/v1/metrics", headers=headers).status_code == 403


def test_metrics_use_observed_sources_with_explicit_scope(staff):
    db, app, client, case_id = staff
    now = utc_now_naive()
    app.state.request_metrics.extend([(now, 200, "/v1/x"), (now, 500, "/v1/x")])
    with db.atomic() as session:
        PersistentAudit(db).append(session, "trace", "owner", "llm.call.completed", {})
        PersistentAudit(db).append(session, "trace2", "owner", "llm.call.failed", {})
        session.add(LLMUsage(window=now.strftime("%Y-%m-%dT%H"), calls=2))
        session.add(SecurityLock(user_id="owner", locked_until=now + timedelta(minutes=1),
                                 first_failed_at=now, reason="test"))
        for index in range(3):
            session.add(SecurityCounter(id=f"counter{index}", user_id="another",
                                        occurred_at=now, reason="risk"))
    response = client.get("/staff/v1/metrics", headers={"X-Staff-Token": "staff-secret"})
    assert response.status_code == 200
    data = response.json()
    assert data["requests"]["count"] == 2
    assert data["requests"]["server_errors"] == 1
    assert data["model"]["failure_rate"] == 0.5
    assert data["user_breakers"]["active"] == 2
    assert data["llm_budget"]["calls"] == 2
    assert "production" not in data["scope"]
    assert "owner" not in response.text


def test_missing_and_malformed_cases_cannot_retry_or_edit(staff):
    db, app, client, case_id = staff
    headers = {"X-Staff-Token": "staff-secret"}
    for action in ["draft", "retry-request"]:
        assert client.post(f"/staff/v1/handoffs/missing/{action}", headers=headers,
                           json={"note": "x"}).status_code == 404
    with db.sessions.begin() as session:
        session.get(HandoffCase, case_id).context_json = "[]"
    for action in ["draft", "retry-request"]:
        assert client.post(f"/staff/v1/handoffs/{case_id}/{action}", headers=headers,
                           json={"note": "x"}).status_code == 409
