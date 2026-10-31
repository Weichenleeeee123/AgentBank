"""Write complete deterministic eval and HTTP self-test evidence; no real providers."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from dataclasses import asdict
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    # The release driver supplies a fresh unique evidence directory.
    out.mkdir(parents=True, exist_ok=True)
    for name in list(os.environ):
        if name.startswith("AGENTBANK_LLM_"):
            os.environ[name] = ""
    os.environ["AGENTBANK_ENV"] = "eval"
    os.environ["AGENTBANK_ADMIN_TOKEN"] = secrets.token_urlsafe(32)
    os.environ["AGENTBANK_STAFF_TOKEN"] = secrets.token_urlsafe(32)
    os.environ["AGENTBANK_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["AGENTBANK_AUDIT_SECRET"] = secrets.token_urlsafe(32)
    os.environ["AGENTBANK_SCHEDULER_ENABLED"] = "false"

    from fastapi.testclient import TestClient
    from agentbank.api.app import create_app
    from agentbank.evals.runner import run_cases

    report = run_cases(Path(__file__).resolve().parents[1] / "evals")
    with (out / "eval-cases.json").open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "total": report.total,
                "passed": report.passed,
                "failed": report.failed,
                "cases": asdict(report)["cases"],
            },
            stream,
            ensure_ascii=False,
            indent=2,
        )
    require(
        report.total == 227 and report.failed == 0,
        "Expected all 227 deterministic cases to pass",
    )
    database = out / "http-self-test.db"
    require(not database.exists(), "Self-test database must be new")
    with TestClient(create_app(f"sqlite:///{database.as_posix()}")) as client:
        response = client.post(
            "/admin/v1/self-test", headers={"X-Admin-Token": os.environ["AGENTBANK_ADMIN_TOKEN"]}
        )
        body = response.json()
        with (out / "http-self-test.json").open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "transport": "FastAPI TestClient HTTP",
                    "method": "POST",
                    "path": "/admin/v1/self-test",
                    "status_code": response.status_code,
                    "body": body,
                },
                stream,
                ensure_ascii=False,
                indent=2,
            )
        require(response.status_code == 200, "Self-test HTTP request failed")
        require(body["case_total"] == 227 and body["case_passed"] == 227, "HTTP eval cases failed")
        require(
            body["total"] == 4 and body["passed"] == 4 and body["failed"] == 0,
            "HTTP self-test gates failed",
        )
        require(
            body["audit_verified"] is True and body["ledger_measured"] is True,
            "Audit or ledger was not verified",
        )
        require(body["unauthorized_ledger_writes"] == 0, "Unauthorized ledger writes detected")
    print(
        "227/227 deterministic cases; HTTP self-test 4/4, audit verified, unauthorized ledger writes 0"
    )


if __name__ == "__main__":
    main()
