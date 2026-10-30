from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_plan_visualization_surface_is_served(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'plan-ui.db'}"))
    assert client.get("/app/plan.html").status_code == 200


def test_plan_graph_projects_persisted_receipt_evidence_without_claiming_unrecorded_checks(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'plan-evidence.db'}"))
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/v1/birthday/plans", headers=headers,
                          json={"event_id": "event_birthday", "reserve_minor": 100_000}).json()
    graph = client.get(f"/v1/plans/{created['plan_id']}/graph", headers=headers).json()
    nodes = {item["logical_id"]: item for item in graph["nodes"]}
    assert nodes["reserve"]["decision_chain"] == [{"stage": "authorization", "status": "pending",
                                                    "receipt_id": created["receipt_id"]}]
    assert nodes["reserve"]["evidence"]["receipt_id"] == created["receipt_id"]
    assert nodes["reserve"]["evidence"]["ledger_entry_ids"] == []


def test_plan_page_renders_persisted_decision_chain_and_live_controls() -> None:
    source = (Path(__file__).parents[2] / "frontend" / "plan.html").read_text(encoding="utf-8")
    assert "/events" in source and "getReader()" in source
    assert "/pause" in source and "/resume" in source and "/cancel" in source
    assert "decision_chain" in source and "evidence" in source
