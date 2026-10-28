from pathlib import Path

from agentbank.evals.runner import run_cases


def test_empty_eval_directory_produces_a_zero_case_report(tmp_path: Path) -> None:
    report = run_cases(tmp_path)

    assert report.total == 0
    assert report.passed == 0
    assert report.failed == 0
    assert report.to_markdown() == "# AgentBank evaluation report\n\nNo cases found.\n"
