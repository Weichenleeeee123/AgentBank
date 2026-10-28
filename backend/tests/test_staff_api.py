from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_staff_handoff_workbench_requires_staff_token_and_cannot_authorize(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTBANK_STAFF_TOKEN", "staff-test")
    app = create_app(f"sqlite:///{tmp_path / 'staff.db'}")
    client = TestClient(app)
    user_token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    handoff = client.post("/v1/handoffs", headers={"Authorization": f"Bearer {user_token}"},
                          json={"operation_type": "transfer", "reason": "U07"})
    case_id = handoff.json()["case_id"]
    forbidden = client.get("/staff/v1/handoffs", headers={"X-Staff-Token": "wrong"})
    assert forbidden.status_code == 403
    listed = client.get("/staff/v1/handoffs", headers={"X-Staff-Token": "staff-test"})
    assert listed.status_code == 200
    assert any(item["id"] == case_id for item in listed.json()["cases"])
    resolved = client.post(f"/staff/v1/handoffs/{case_id}/resolve", headers={"X-Staff-Token": "staff-test"}, json={"note": "已通知用户重新验证"})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
