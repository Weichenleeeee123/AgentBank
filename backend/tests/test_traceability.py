from pathlib import Path

from agentbank.evals.runner import EvaluationCase, EvaluationReport
from agentbank.evals.traceability import (
    build_traceability_artifact,
    build_traceability_manifest,
    validate_traceability_manifest,
)


ROOT = Path(__file__).resolve().parents[2]


def test_manifest_covers_all_capability_journeys_with_real_code_and_test_links() -> None:
    manifest = build_traceability_manifest(ROOT)

    assert len(manifest["cases"]) == 99
    assert len({item["case_id"] for item in manifest["cases"]}) == 99
    assert validate_traceability_manifest(manifest, ROOT) == []
    assert all(item["spec_sections"] for item in manifest["cases"])
    assert all(item["implementation"]["path"] for item in manifest["cases"])
    assert all(item["test"]["path"] and item["test"]["function"] for item in manifest["cases"])
    assert all(item["runner"]["function"] == "_run_capability_detailed" for item in manifest["cases"])
    assert all(item["policy"]["path"] and item["terminal_evidence"] for item in manifest["cases"])


def test_boundary_fixture_inventory_marks_missing_capability_specific_tests() -> None:
    manifest = build_traceability_manifest(ROOT)
    boundary = [item for item in manifest["cases"] if item["variant"] == 3]

    assert len(boundary) == 33
    assert all(item["boundary_fixture"]["status"] in {"mapped", "missing"} for item in boundary)
    assert all(item["boundary_fixture"].get("test_function") for item in boundary
               if item["boundary_fixture"]["status"] == "mapped")


def test_artifact_binds_report_to_source_and_case_audit_evidence() -> None:
    report = EvaluationReport([
        EvaluationCase("CAP-T01-normal", "capability", "passed", {
            "record_id": "rec-1",
            "audit_event": "capability.committed",
            "terminal_state": {"operation_id": "op-1", "ledger_entry_ids": ["led-1", "led-2"]},
        }),
        EvaluationCase("CAP-T01-boundary", "capability", "passed", {
            "terminal_state": {"boundary": "ambiguous_payee_denied"},
        }),
    ])

    artifact = build_traceability_artifact(report, ROOT)

    assert artifact["source"]["git_revision"]
    assert isinstance(artifact["source"]["dirty"], bool)
    assert artifact["suite"]["content_sha256"]
    normal, boundary = artifact["cases"]
    assert normal["evidence"]["audit_event"] == "capability.committed"
    assert normal["evidence"]["record_id"] == "rec-1"
    assert normal["evidence"]["operation_ids"] == ["op-1"]
    assert normal["evidence"]["ledger_entry_ids"] == ["led-1", "led-2"]
    assert boundary["evidence"]["audit_event"] is None
    assert artifact["prompts"]


def test_validator_reports_missing_source_files_without_raising() -> None:
    manifest = {"version": "test", "cases": [{
        "case_id": "CAP-BOGUS-normal",
        "spec_sections": ["§3"],
        "implementation": {"path": "backend/does-not-exist.py"},
        "runner": {"path": "backend/agentbank/evals/runner.py", "function": "no_such_runner"},
        "test": {"path": "backend/tests/test_traceability.py", "function": "no_such_test"},
    }]}

    errors = validate_traceability_manifest(manifest, ROOT)

    assert any("backend/does-not-exist.py" in error for error in errors)
    assert any("no_such_runner" in error for error in errors)
    assert any("no_such_test" in error for error in errors)
