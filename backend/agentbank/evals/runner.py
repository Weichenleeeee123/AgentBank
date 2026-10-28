"""Run YAML cases without making LLM calls; M0 deliberately supports an empty set."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]


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
    # Case execution is introduced in M1; M0 only proves discovery and reporting.
    return EvaluationReport(total=len(loaded), passed=0, failed=len(loaded))
