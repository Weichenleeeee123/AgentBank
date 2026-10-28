from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_plan_visualization_surface_is_served(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'plan-ui.db'}"))
    assert client.get("/app/plan.html").status_code == 200
