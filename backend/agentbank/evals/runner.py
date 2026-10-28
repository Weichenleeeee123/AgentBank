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
    loaded: list[dict[str, Any]] = []
    for path in cases:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("kind") == "capability_matrix":
            loaded.extend({"kind": "capability", "capability_id": capability_id, "variant": variant}
                          for capability_id in CAPABILITY_IDS for variant in range(1, 4))
        elif isinstance(document, dict) and document.get("kind") == "m3_matrix":
            loaded.extend({"kind": "m3", "case_id": case_id} for case_id in M3_CASE_IDS)
        elif isinstance(document, list):
            loaded.extend(document)
        else:
            loaded.append(document)
    passed = sum(1 for case in loaded if _run_case(case))
    return EvaluationReport(total=len(loaded), passed=passed, failed=len(loaded) - passed)


def _run_case(case: dict[str, Any]) -> bool:
    if case.get("kind") == "fraud":
        assessment = FraudDetector().evaluate(str(case.get("text", "")),
                                               relation=dict(case.get("relation", {})),
                                               behavior=dict(case.get("behavior", {})),
                                               classifier_failed=bool(case.get("classifier_failed", False)))
        return assessment.level.value == case.get("expected_level")
    if case.get("kind") == "capability":
        from agentbank.bank.capabilities_full import CapabilityService
        from agentbank.db import Database
        from agentbank.seed.loader import load_seed

        database = Database("sqlite:///:memory:")
        load_seed(database)
        service = CapabilityService(database)
        capability_id = str(case["capability_id"])
        payload = _capability_payload(capability_id, int(case.get("variant", 1)))
        if capability_id in {"W01", "W06"}:
            service.execute("user_001", "W03", {"answers": [1] * 10}, f"eval-setup-{capability_id}")
        if capability_id == "W04":
            service.execute("user_001", "W03", {"answers": [2] * 10}, "eval-setup-W04")
        if capability_id == "C06":
            service.execute("user_001", "C05", {"card_id": "card_001", "assurance": "mfa"}, f"eval-setup-C06-{case.get('variant')}")
        try:
            result = service.execute("user_001", capability_id, payload, f"eval-{capability_id}-{case.get('variant')}")
        except Exception:
            return False
        return bool(result.record_id and result.evidence.get("audit_event"))
    if case.get("kind") == "m3":
        from agentbank.bank.capabilities_full import CapabilityService
        from agentbank.db import Database
        from agentbank.seed.loader import load_seed
        from agentbank.security.breaker import CircuitBreaker
        from agentbank.security.cooling import CoolingPeriodService
        from agentbank.security.fragments import ReplyFragmentChecker
        from agentbank.security.fraud import FraudLevel
        from agentbank.workflow.recovery import SagaRunner, UnknownRecoveryService
        from agentbank.db import Operation

        database = Database("sqlite:///:memory:")
        load_seed(database)
        case_id = str(case["case_id"])
        if case_id == "M3-FRAUD-HIGH":
            return FraudDetector().evaluate("转到安全账户并马上转账", relation={}, behavior={}).level is FraudLevel.high
        if case_id == "M3-FRAUD-LOW":
            return FraudDetector().evaluate("给公安食堂供应商支付货款", relation={}, behavior={}).level is FraudLevel.low
        if case_id == "M3-FRAGMENT":
            try:
                ReplyFragmentChecker().validate([{"type": "text", "text": "本月支出是100元"}])
            except Exception:
                return True
            return False
        if case_id == "M3-COOLING":
            cooling = CoolingPeriodService(database).start("user_001", "p", 100)
            return CoolingPeriodService(database).release(cooling.id, now=cooling.expires_at, reconfirmed=True)
        if case_id == "M3-BREAKER":
            breaker = CircuitBreaker(database)
            for _ in range(3):
                breaker.record_block("user_001", "test")
            try:
                CapabilityService(database).execute("user_001", "S02", {}, "m3-breaker")
            except Exception:
                return True
            return False
        if case_id == "M3-SAGA":
            saga_result = SagaRunner().run([("a", lambda: None, lambda: None), ("b", lambda: (_ for _ in ()).throw(RuntimeError()), lambda: None)])
            return saga_result.status == "partially_completed"
        if case_id == "M3-UNKNOWN":
            with database.sessions.begin() as session:
                session.add(Operation(id="op_m3", user_id="user_001", idempotency_key="idem_m3", plan_id="p", node_id="n", status="unknown", amount_minor=1, recipient_id="r"))
            return UnknownRecoveryService(database).recover("op_m3").status == "processing"
        return False
    return str(case.get("expected", "pass")) == "pass"


CAPABILITY_IDS = [
    "Q01", "Q02", "T01", "T02", "T03", "T04", "T05", "B01", "B02", "B03", "B04",
    "W01", "W02", "W03", "W04", "W05", "W06", "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08",
    "S01", "S02", "S03", "X01", "X02", "X03", "X04", "X05",
]
M3_CASE_IDS = ["M3-FRAUD-HIGH", "M3-FRAUD-LOW", "M3-FRAGMENT", "M3-COOLING", "M3-BREAKER", "M3-SAGA", "M3-UNKNOWN"]


def _capability_payload(capability_id: str, variant: int) -> dict[str, Any]:
    if capability_id in {"Q01", "B02", "B04", "W01", "W02", "W06", "S01", "X01"}:
        return {}
    if capability_id == "Q02":
        return {"limit": 10 * variant}
    if capability_id == "T01":
        return {"recipient_name": "房东", "amount_minor": 100 * variant}
    if capability_id == "T02":
        return {"phone": "13800000001"}
    if capability_id == "T03":
        return {"alias": "房东"}
    if capability_id == "T04":
        return {"max_uses": variant}
    if capability_id == "T05":
        return {"total_minor": 10_000 + variant}
    if capability_id == "B01":
        return {"dsl": {"source": "transactions", "group_by": ["month"], "limit": 10}}
    if capability_id == "B03":
        return {"start_month": "2026-05", "end_month": "2026-07"}
    if capability_id == "W03":
        return {"answers": [variant] * 10}
    if capability_id == "W04":
        return {"product_id": "product_R1", "amount_minor": 1_000 * variant, "assurance": "mfa"}
    if capability_id == "W05":
        return {"order_id": f"eval-order-{variant}", "assurance": "mfa"}
    if capability_id in {"C01"}:
        return {"virtual": variant != 2, **({"assurance": "mfa"} if variant == 2 else {})}
    if capability_id == "C02":
        return {"limit_minor": 100_000 - variant * 1_000}
    if capability_id in {"C03", "C04"}:
        return {"restriction": "online", **({"assurance": "mfa"} if capability_id == "C04" else {})}
    if capability_id in {"C05", "C06", "C07", "C08"}:
        data: dict[str, Any] = {"card_id": "card_001", **({"new_pin": f"{1234 + variant}"} if capability_id == "C08" else {})}
        if capability_id in {"C05", "C06", "C08"}:
            data["assurance"] = "mfa"
        return data
    if capability_id == "S02":
        return {"days": variant}
    if capability_id == "S03":
        return {"subscription_id": "sub_video"}
    if capability_id == "X02":
        return {"amount_minor": 1_000 * variant}
    if capability_id in {"X03", "X04"}:
        return {"delivery_date": "2026-11-13"}
    if capability_id == "X05":
        return {"card_id": "card_001", "mfa_verified": True}
    return {}
