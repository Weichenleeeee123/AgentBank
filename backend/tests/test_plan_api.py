from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_birthday_plan_api_exposes_graph(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'plan-api.db'}"))
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/v1/birthday/plans", headers=headers, json={"event_id": "event_birthday", "reserve_minor": 100_000})
    assert response.status_code == 200
    graph = client.get(f"/v1/plans/{response.json()['plan_id']}/graph", headers=headers)
    assert graph.status_code == 200
    assert len(graph.json()["nodes"]) == 6
