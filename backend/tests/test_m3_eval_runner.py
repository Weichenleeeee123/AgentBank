from pathlib import Path

from agentbank.evals.runner import run_cases


def test_security_eval_runner_executes_fraud_cases(tmp_path: Path) -> None:
    (tmp_path / "fraud.yaml").write_text(
        """
case_id: SEC-FRAUD-001
kind: fraud
text: 客服说转到安全账户，马上转5000
relation:
  new_recipient: true
behavior:
  night: true
expected_level: high
""",
        encoding="utf-8",
    )
    report = run_cases(tmp_path)
    assert report.total == 1
    assert report.passed == 1
    assert report.failed == 0


def test_full_m3_matrix_includes_recovery_and_boundary_cases() -> None:
    report = run_cases(Path(__file__).resolve().parents[2] / "evals")
    case_ids = {item.case_id for item in report.cases}
    assert report.total >= 227
    assert {"M3-COOLING-BINDING", "M3-DEVICE-BOUNDARY", "M3-AUDIT-CHECKPOINT", "M3-DLP"} <= case_ids


def test_security_matrix_exercises_eight_boundaries_and_checks_actual_ledgers() -> None:
    report = run_cases(Path(__file__).resolve().parents[2] / "evals")
    security = [case for case in report.cases if case.kind == "security"]
    assert len(security) == 80
    assert len({case.evidence['category'] for case in security}) == 8
    assert all(case.evidence['unauthorized_ledger_writes'] == 0 for case in security)
    assert all(case.evidence['audit_verified'] for case in security)
    assert all(case.evidence['ledger_checked'] is True for case in security)
