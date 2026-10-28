from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_pwa_shell_is_served_by_app(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'pwa.db'}"))
    response = client.get("/app/")
    assert response.status_code == 200
    assert "AgentBank" in response.text
    assert client.get("/app/app.js").status_code == 200
