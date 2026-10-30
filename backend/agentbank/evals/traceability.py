"""Executable traceability for the M2 capability acceptance suite.

The manifest is deliberately data-only: it points at the existing runner and
tests, and never turns a missing audit or ledger identifier into a claim.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from agentbank.evals.runner import CAPABILITY_IDS, CAP_JOURNEYS, EvaluationReport


MANIFEST_VERSION = "m2-cap-traceability-v1"
MATRIX_PATH = Path("evals/m2_m3_capability_matrix.yaml")
RUNNER_PATH = Path("backend/agentbank/evals/runner.py")
IMPLEMENTATION_PATH = Path("backend/agentbank/bank/capabilities_full.py")
TEST_PATH = Path("backend/tests/test_cap_journeys.py")
PROMPT_DIR = Path("backend/agentbank/agent/prompts")
POLICY_PATH = Path("backend/agentbank/policy/rules.yaml")

# PLAN §11's deterministic fixture intent is recorded even where there is no
# capability-specific pytest yet.  Empty entries are honest gaps for follow-up.
BOUNDARY_FIXTURES: dict[str, tuple[str, str]] = {
    "Q01": ("mapped", "test_q01_returns_only_the_authenticated_subject_balance"),
    "Q02": ("mapped", "test_transaction_end_date_includes_timestamp_rows_on_that_date"),
    "T01": ("mapped", "test_recipient_resolver_requires_disambiguation_and_supports_phone"),
    "T02": ("mapped", "test_t02_unknown_phone_is_denied_without_creating_capability_record"),
    "T03": ("mapped", "test_t02_unknown_phone_and_t03_ambiguous_alias_never_prepare"),
    "T04": ("mapped", "test_t04_runs_two_periods_and_deduplicates_dispatch"),
    "B01": ("mapped", "test_b01_groups_reconcile_to_transaction_ids"),
    "B02": ("mapped", "test_b02_each_anomaly_has_reason_and_transaction_evidence"),
    "B03": ("mapped", "test_b03_month_report_separates_flows"),
    "B04": ("mapped", "test_b04_year_report_returns_calendar_year_aggregate"),
    "T05": ("mapped", "test_t05_duplicate_participants_are_rejected_before_bill_creation"),
    "W01": ("mapped", "test_w01_missing_profile_and_w02_unknown_product_are_denied"),
    "W02": ("mapped", "test_w01_missing_profile_and_w02_unknown_product_are_denied"),
    "W04": ("mapped", "test_w04_unsuitable_product_is_denied_without_debit"),
    "W06": ("mapped", "test_w06_positions_are_scoped_to_the_requesting_user"),
    "W03": ("mapped", "test_expired_risk_profile_cannot_recommend_or_subscribe"),
    "W05": ("mapped", "test_w05_rejects_non_subscribed_order_and_insufficient_position"),
    "C01": ("mapped", "test_c01_rejected_application_can_be_retried_as_new_application"),
    "C02": ("mapped", "test_c02_limit_above_product_ceiling_is_denied"),
    "C03": ("mapped", "test_c02_limits_and_c03_c04_restrictions_affect_payment"),
    "C04": ("mapped", "test_c02_limits_and_c03_c04_restrictions_affect_payment"),
    "C05": ("mapped", "test_c05_c06_c07_state_guards_and_c08_secret_not_persisted"),
    "C06": ("mapped", "test_c05_c06_c07_state_guards_and_c08_secret_not_persisted"),
    "C07": ("mapped", "test_c05_c06_c07_state_guards_and_c08_secret_not_persisted"),
    "C08": ("mapped", "test_c08_prepare_record_does_not_persist_pin"),
    "S02": ("mapped", "test_s02_deduplicates_same_active_reminder_across_request_keys"),
    "S01": ("mapped", "test_s01_evidence_is_user_scoped_and_distinguishes_confirmed_suspected"),
    "S03": ("mapped", "test_s03_cancels_subscription_and_revokes_mandate"),
    "X01": ("mapped", "test_x01_uses_trusted_event_and_does_not_spend"),
    "X02": ("mapped", "test_x02_reserve_then_release_preserves_ledger_balance"),
    "X03": ("mapped", "test_x03_x04_orders_are_separate_and_x05_freeze_is_preserved_on_failed_mfa"),
    "X04": ("mapped", "test_x03_x04_orders_are_separate_and_x05_freeze_is_preserved_on_failed_mfa"),
    "X05": ("mapped", "test_x03_x04_orders_are_separate_and_x05_freeze_is_preserved_on_failed_mfa"),
}


def _spec_sections(capability_id: str) -> list[str]:
    group = capability_id[0]
    sections = {"Q": "§3.1", "T": "§3.2", "B": "§3.3", "W": "§3.4",
                "C": "§3.5", "S": "§3.6", "X": "§3.6"}
    return [sections.get(group, "§3"), "§6", "§7", "§11"]


def build_traceability_manifest(root: Path) -> dict[str, Any]:
    """Build the complete 33 x 3 manifest from the canonical runner inventory."""
    matrix = root / MATRIX_PATH
    matrix_data = yaml.safe_load(matrix.read_text(encoding="utf-8")) if matrix.is_file() else {}
    terminal_by_capability = {
        str(item.get("capability_id")): str(item.get("terminal_evidence", ""))
        for item in matrix_data.get("traceability", [])
        if isinstance(item, dict)
    } if isinstance(matrix_data, dict) else {}
    cases: list[dict[str, Any]] = []
    for capability_id in CAPABILITY_IDS:
        for variant, journey in enumerate(CAP_JOURNEYS[capability_id], 1):
            status, function = BOUNDARY_FIXTURES.get(capability_id, ("missing", ""))
            boundary: dict[str, Any] = {"status": status,
                                        "fixture": "PLAN-§11 deterministic seed",
                                        "test_path": "backend/tests/test_cap_journeys.py"}
            if variant == 3:
                boundary["test_function"] = function or None
                if status == "missing":
                    boundary["note"] = "No capability-specific boundary test is currently mapped."
            else:
                boundary = {"status": "not_applicable", "fixture": None, "test_path": None,
                            "test_function": None}
            cases.append({
                "case_id": journey,
                "capability_id": capability_id,
                "variant": variant,
                "journey": ("normal" if variant == 1 else "colloquial" if variant == 2 else "boundary"),
                "spec_sections": _spec_sections(capability_id),
                "implementation": {"path": str(IMPLEMENTATION_PATH).replace("\\", "/"),
                                   "symbol": "CapabilityService"},
                "runner": {"path": str(RUNNER_PATH).replace("\\", "/"),
                           "function": "_run_capability_detailed"},
                "test": {"path": str(TEST_PATH).replace("\\", "/"),
                         "function": "test_all_capabilities_have_three_independent_journeys_with_evidence"},
                "policy": {"path": str(POLICY_PATH).replace("\\", "/"),
                           "evidence": "policy engine evaluates capability security level before dispatch"},
                "terminal_evidence": terminal_by_capability.get(capability_id, ""),
                "boundary_fixture": boundary,
            })
    return {"version": MANIFEST_VERSION, "matrix": str(MATRIX_PATH).replace("\\", "/"),
            "case_count": len(cases), "cases": cases}


def _python_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def validate_traceability_manifest(manifest: dict[str, Any], root: Path) -> list[str]:
    """Return actionable validation errors; do not fail by raising on bad metadata."""
    errors: list[str] = []
    entries = manifest.get("cases")
    expected = {journey for cid in CAPABILITY_IDS for journey in CAP_JOURNEYS[cid]}
    if not isinstance(entries, list):
        return ["manifest cases must be a list"]
    actual = [item.get("case_id") for item in entries if isinstance(item, dict)]
    if set(actual) != expected:
        errors.append(f"case IDs incomplete: expected {len(expected)}, found {len(set(actual))}")
    if len(actual) != len(set(actual)):
        errors.append("case IDs must be unique")
    matrix = root / MATRIX_PATH
    if not matrix.is_file():
        errors.append(f"missing case matrix {MATRIX_PATH.as_posix()}")
    else:
        try:
            document = yaml.safe_load(matrix.read_text(encoding="utf-8"))
            matrix_ids = {str(item.get("capability_id")) for item in document.get("traceability", [])}
            if matrix_ids != set(CAPABILITY_IDS):
                errors.append("matrix capability IDs do not match the runner inventory")
        except (AttributeError, TypeError, yaml.YAMLError) as error:
            errors.append(f"invalid case matrix: {error}")
    for item in entries:
        if not isinstance(item, dict):
            errors.append("case entry must be a mapping")
            continue
        for section in ("spec_sections", "implementation", "runner", "test"):
            if not item.get(section):
                errors.append(f"{item.get('case_id', '<unknown>')} missing {section}")
        for section in ("implementation", "runner", "test"):
            target = item.get(section, {})
            path = root / str(target.get("path", ""))
            if not path.is_file():
                errors.append(f"{item.get('case_id')}: missing {target.get('path')}")
            else:
                symbol = target.get("function") or target.get("symbol")
                if not symbol or symbol not in _python_symbols(path):
                    errors.append(f"{item.get('case_id')}: missing symbol {symbol}")
    return errors


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _prompt_versions(root: Path) -> list[dict[str, str]]:
    result = []
    for path in sorted((root / PROMPT_DIR).glob("*.md")):
        result.append({"path": str(path.relative_to(root)).replace("\\", "/"),
                       "version": _sha256(path)[:12], "sha256": _sha256(path)})
    return result


def _find_values(value: Any, names: set[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {name: [] for name in names}
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text in names and child is not None:
                values = child if isinstance(child, list) else [child]
                found[key_text].extend(str(item) for item in values if item not in ("", None))
            nested = _find_values(child, names)
            for name, items in nested.items():
                found[name].extend(items)
    elif isinstance(value, list):
        for child in value:
            nested = _find_values(child, names)
            for name, items in nested.items():
                found[name].extend(items)
    return found


def build_traceability_artifact(report: EvaluationReport, root: Path) -> dict[str, Any]:
    """Bind a report to source, suite, prompt, and available audit evidence."""
    manifest = build_traceability_manifest(root)
    evidence_keys = {"audit_event", "audit_event_id", "event_id", "record_id", "operation_id",
                     "ledger_entry_id", "ledger_entry_ids", "operation_ids", "audit_record_id"}
    cases = []
    for item in report.cases:
        values = _find_values(item.evidence, evidence_keys)
        evidence = {
            "audit_event": (values["audit_event"] + values["audit_event_id"] + values["event_id"] + values["audit_record_id"])[0]
            if values["audit_event"] + values["audit_event_id"] + values["event_id"] + values["audit_record_id"] else None,
            "record_id": values["record_id"][0] if values["record_id"] else None,
            "operation_ids": sorted(set(values["operation_id"] + values["operation_ids"])),
            "ledger_entry_ids": sorted(set(values["ledger_entry_id"] + values["ledger_entry_ids"])),
        }
        cases.append({"case_id": item.case_id, "kind": item.kind, "outcome": item.outcome,
                      "errors": list(item.errors), "evidence": evidence,
                      "terminal_state": item.evidence.get("terminal_state")})
    matrix = root / MATRIX_PATH
    return {"version": MANIFEST_VERSION, "source": {"git_revision": _git(root, "rev-parse", "HEAD"),
             "dirty": bool(_git(root, "status", "--porcelain"))},
            "suite": {"path": str(MATRIX_PATH).replace("\\", "/"),
                      "content_sha256": _sha256(matrix) if matrix.is_file() else None,
                      "version": _matrix_version(matrix)},
            "prompts": _prompt_versions(root), "manifest": manifest,
            "report": {"total": report.total, "passed": report.passed, "failed": report.failed},
            "cases": cases}


def _matrix_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return str(loaded.get("version")) if isinstance(loaded, dict) and loaded.get("version") else None

