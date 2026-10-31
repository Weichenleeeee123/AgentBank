"""Regression checks for the actual release boundary, not runtime application behavior."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_release", ROOT / "scripts/release.py")
assert spec and spec.loader
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_business_timezone_is_available_in_clean_runtime() -> None:
    from zoneinfo import ZoneInfo

    assert ZoneInfo("Asia/Shanghai").key == "Asia/Shanghai"


def test_archive_excludes_local_credentials_state_and_generated_files() -> None:
    for path in (
        ".env",
        ".env.local",
        ".tmp/local-demo/eval.env",
        "backend/x.db",
        "backend/x.db-wal",
        "backend/x.db.checkpoints.key",
        "backend/__pycache__/x.pyc",
        "backend/agentbank.egg-info/PKG-INFO",
        "frontend/node_modules/x.js",
        "docs/run.log",
    ):
        assert not release.included(path), path
    for path in (
        ".env.example",
        "requirements.lock",
        "requirements-dev.lock",
        "backend/agentbank/agent/prompts/planner.md",
        "frontend/app.js",
        "evals/security_cases.yaml",
        "docs/LOCAL-RELEASE.md",
        "tests/test_m4_release.py",
        ".github/workflows/ci.yml",
    ):
        assert release.included(path), path


def test_gate_inventory_covers_release_quality_and_every_frontend_file(tmp_path: Path) -> None:
    commands = dict(release.gate_commands(tmp_path))
    assert {
        "dependencies",
        "ruff",
        "mypy",
        "python-tests",
        "frontend-tests",
        "eval-and-http",
    } <= commands.keys()
    assert commands["ruff"][-3:] == ["backend", "scripts", "tests"]
    assert commands["mypy"][-1] == "backend/agentbank"
    assert commands["python-tests"][3:5] == ["backend/tests", "tests"]
    assert commands["python-tests"][-2:] == ["--basetemp", str(tmp_path / "pytest")]
    for path in (ROOT / "tests/frontend").glob("*.test.mjs"):
        assert path.relative_to(ROOT).as_posix() in commands["frontend-tests"]
    for path in (ROOT / "frontend").rglob("*"):
        if path.suffix in {".js", ".mjs"} and "node_modules" not in path.parts:
            assert ["node", "--check", path.relative_to(ROOT).as_posix()] in commands.values()
    assert commands["eval-and-http"] == [
        release.sys.executable,
        "scripts/verify_evals.py",
        "--output",
        str(tmp_path),
    ]


def test_secret_scan_catches_credentials_without_printing_values(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(release, "ROOT", tmp_path)
    (tmp_path / "source.txt").write_text("sk-" + "proj-" + "a" * 32, encoding="utf-8")
    assert release.secret_findings(["source.txt"]) == ["source.txt"]


def test_compose_requires_independent_audit_and_staff_secrets() -> None:
    services = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"))[
        "services"
    ]
    assert services["public"]["environment"]["AGENTBANK_ADMIN_TOKEN"] == ""
    assert services["public"]["environment"]["AGENTBANK_STAFF_TOKEN"] == ""
    assert "PUBLIC_AUDIT_SECRET:?" in services["public"]["environment"]["AGENTBANK_AUDIT_SECRET"]
    assert "EVAL_AUDIT_SECRET:?" in services["eval"]["environment"]["AGENTBANK_AUDIT_SECRET"]
    assert "EVAL_STAFF_TOKEN:?" in services["eval"]["environment"]["AGENTBANK_STAFF_TOKEN"]


def test_docker_build_context_actually_excludes_credentials() -> None:
    # With context '..', deploy/.dockerignore alone is ignored by Docker.
    ignored = (ROOT / "deploy/Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", ".tmp", "*.db", "*.checkpoints.key"} <= set(ignored)


def test_env_example_has_unique_keys_and_no_known_secrets() -> None:
    keys = [
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(keys) == len(set(keys))
    assert "AGENTBANK_AUDIT_HMAC_KEY" not in keys
