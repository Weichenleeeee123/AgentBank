from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "deploy" / "docker-compose.yml"
DOCKERFILE_PATH = ROOT / "deploy" / "Dockerfile"


def test_compose_declares_two_isolated_instances_and_healthchecks() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"public", "eval"}
    assert services["public"]["ports"] == ["8000:8000"]
    assert services["eval"]["ports"] == ["8001:8000"]
    assert services["public"]["environment"]["AGENTBANK_DATABASE_URL"] == "sqlite:////data/public.db"
    assert services["eval"]["environment"]["AGENTBANK_DATABASE_URL"] == "sqlite:////data/eval.db"
    assert "PUBLIC_SESSION_SECRET:?set PUBLIC_SESSION_SECRET" in services["public"]["environment"]["AGENTBANK_SESSION_SECRET"]
    assert "EVAL_SESSION_SECRET:?set EVAL_SESSION_SECRET" in services["eval"]["environment"]["AGENTBANK_SESSION_SECRET"]
    assert services["eval"]["environment"]["AGENTBANK_ADMIN_TOKEN"] == "${EVAL_ADMIN_TOKEN:?set EVAL_ADMIN_TOKEN}"

    for name in ("public", "eval"):
        healthcheck = services[name]["healthcheck"]
        assert healthcheck["test"][:2] == ["CMD", "python"]
        assert "/healthz" in healthcheck["test"][-1]
        assert healthcheck["retries"] >= 1

    assert compose["volumes"] == {"public_data": None, "eval_data": None}


def test_dockerfile_builds_the_same_application_used_by_compose() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in dockerfile
    assert "COPY pyproject.toml README.md ./" in dockerfile
    assert "COPY backend ./backend" in dockerfile
    assert "COPY frontend ./frontend" in dockerfile
    assert '"agentbank.api.run:app"' in dockerfile
    assert '"--host", "0.0.0.0"' in dockerfile
    assert '"--port", "8000"' in dockerfile
