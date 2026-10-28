"""Run YAML cases without making LLM calls; M0 deliberately supports an empty set."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from agentbank.security.fraud import FraudDetector


@dataclass(frozen=True)
class EvaluationReport:
    total: int
    passed: int
    failed: int

    def to_markdown(self) -> str:
        if self.total == 0:
            return "# AgentBank evaluation report\n\nNo cases found.\n"
        return (
            "# AgentBank evaluation report\n\n"
            f"- Total: {self.total}\n- Passed: {self.passed}\n- Failed: {self.failed}\n"
        )


def run_cases(case_dir: Path) -> EvaluationReport:
    cases = sorted(case_dir.glob("*.yaml"))
    loaded = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in cases]
    passed = sum(1 for case in loaded if _run_case(case))
    return EvaluationReport(total=len(loaded), passed=passed, failed=len(loaded) - passed)


def _run_case(case: dict[str, Any]) -> bool:
    if case.get("kind") == "fraud":
        assessment = FraudDetector().evaluate(str(case.get("text", "")),
                                               relation=dict(case.get("relation", {})),
                                               behavior=dict(case.get("behavior", {})),
                                               classifier_failed=bool(case.get("classifier_failed", False)))
        return assessment.level.value == case.get("expected_level")
    return str(case.get("expected", "pass")) == "pass"
