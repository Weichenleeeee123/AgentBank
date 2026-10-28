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
