from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def test_eval_and_device_surfaces_are_served(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'surfaces.db'}"))
    assert client.get("/app/eval.html").status_code == 200
    assert client.get("/app/eval.js").status_code == 200
    assert client.get("/app/device.html").status_code == 200
