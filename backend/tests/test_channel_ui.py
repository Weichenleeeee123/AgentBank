from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_channel_routes_support_the_frontend_confirmation_and_inbox_flow(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'channel-ui.db'}"))
    session = client.post("/v1/sessions", json={"user_id": "user_001"})
    headers = {"Authorization": f"Bearer {session.json()['access_token']}"}

    prepared = client.post("/v1/conversations/ui/messages", headers=headers,
                           json={"text": "给房东转20元"})
    assert prepared.status_code == 200
    body = prepared.json()
    assert body["kind"] == "confirmation"
    assert body["record_id"] and body["display_hash"]
    assert body["data"]["receipt_id"]
    assert body["fragments"]

    confirmed = client.post(f"/v1/conversations/ui/records/{body['record_id']}/confirm",
                            headers=headers,
                            json={"receipt_id": body["data"]["receipt_id"],
                                  "display_hash": body["display_hash"]})
    assert confirmed.status_code == 200
    assert confirmed.json()["kind"] in {"result", "mfa"}
    assert confirmed.json()["record_id"] == body["record_id"]

    record = client.get(f"/v1/capability-records/{body['record_id']}", headers=headers)
    assert record.status_code == 200
    assert record.json()["record_id"] == body["record_id"]
    assert client.get("/v1/notifications", headers=headers).status_code == 200


def test_channel_ui_never_injects_api_text_as_html() -> None:
    source = (Path(__file__).parents[2] / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source
    assert "textContent" in source
    assert 'api("/v1/notifications")' in source
    assert "api(`/v1/capability-records/${recordId}`)" in source
    assert 'api("/v1/capabilities/C08"' in source


def test_plan_page_discovers_plans_and_renders_graph_as_text() -> None:
    source = (Path(__file__).parents[2] / "frontend" / "plan.html").read_text(encoding="utf-8")
    assert 'fetch("/v1/plans"' in source
    assert 'fetch(`/v1/plans/${encodeURIComponent(id)}/graph`' in source
    assert "innerHTML" not in source
    assert 'new Option(`${plan.plan_id} · ${displayValue(plan.status)} · 修订 ${plan.revision}`, plan.plan_id)' in source


def test_plan_list_is_authenticated_and_scoped_to_the_selected_account(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'plan-list.db'}"))
    first = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    second = client.post("/v1/sessions", json={"user_id": "user_002"}).json()["access_token"]
    first_headers = {"Authorization": f"Bearer {first}"}
    second_headers = {"Authorization": f"Bearer {second}"}
    created = client.post("/v1/birthday/plans", headers=first_headers,
                          json={"event_id": "event_birthday"})
    assert created.status_code == 200

    listed = client.get("/v1/plans", headers=first_headers)
    assert [item["plan_id"] for item in listed.json()["plans"]] == [created.json()["plan_id"]]
    assert client.get("/v1/plans", headers=second_headers).json()["plans"] == []
    assert client.get("/v1/plans").status_code == 401
