"""Deterministic M2/M3 evaluation runner.

Capability cases are executable journeys.  They use the real prepare/confirm/OTP
path (or the deterministic Agent orchestrator for colloquial journeys) and assert
persistent business state, rather than only checking that a record was created.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentbank.security.fraud import FraudDetector


@dataclass(frozen=True)
class EvaluationReport:
    cases: list["EvaluationCase"] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(item.outcome == "passed" for item in self.cases)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def to_markdown(self) -> str:
        if self.total == 0:
            return "# AgentBank evaluation report\n\nNo cases found.\n"
        return (
            "# AgentBank evaluation report\n\n"
            f"- Total: {self.total}\n- Passed: {self.passed}\n- Failed: {self.failed}\n"
        )


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    kind: str
    outcome: str
    evidence: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def run_cases(case_dir: Path) -> EvaluationReport:
    cases = sorted(case_dir.glob("*.yaml"))
    loaded: list[dict[str, Any]] = []
    for path in cases:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("kind") == "capability_matrix":
            loaded.extend({"kind": "capability", "capability_id": capability_id, "variant": variant,
                           "journey": CAP_JOURNEYS[capability_id][variant - 1]}
                          for capability_id in CAPABILITY_IDS for variant in range(1, 4))
        elif isinstance(document, dict) and document.get("kind") == "m3_matrix":
            matrix_cases = document.get("cases")
            case_ids = matrix_cases if isinstance(matrix_cases, list) and matrix_cases else M3_CASE_IDS
            loaded.extend({"kind": "m3", "case_id": str(case_id)} for case_id in case_ids)
        elif isinstance(document, dict) and document.get("kind") == "security_matrix":
            loaded.extend(_security_matrix_cases())
        elif isinstance(document, list):
            loaded.extend(document)
        else:
            loaded.append(document)
    outcomes: list[EvaluationCase] = []
    known_kinds = {"fraud", "capability", "m3", "security"}
    for index, case in enumerate(loaded):
        if not isinstance(case, dict):
            raise ValueError(f"case {index} must be a mapping")
        kind = str(case.get("kind", ""))
        if kind not in known_kinds:
            raise ValueError(f"unknown evaluation case kind: {kind or '<missing>'}")
        case_id = str(case.get("case_id") or case.get("journey") or f"case-{index}")
        try:
            if kind == "security":
                from agentbank.evals.security import run_security_case
                passed, evidence, errors = run_security_case(case)
            elif kind == "capability":
                passed, evidence, errors = _run_capability_detailed(case)
            else:
                passed = _run_case(case)
                evidence, errors = ({"kind": kind}, []) if passed else ({}, ["case assertion failed"])
        except Exception as error:  # an evaluation error is a red case, never a green case
            passed, evidence, errors = False, {}, [f"{type(error).__name__}: {error}"]
        outcomes.append(EvaluationCase(case_id, kind, "passed" if passed else "failed", evidence, errors))
    return EvaluationReport(outcomes)


def _run_case(case: dict[str, Any]) -> bool:
    if case.get("kind") == "capability":
        passed, _, _ = _run_capability_detailed(case)
        return passed
    if case.get("kind") == "fraud":
        assessment = FraudDetector().evaluate(str(case.get("text", "")),
                                               relation=dict(case.get("relation", {})),
                                               behavior=dict(case.get("behavior", {})),
                                               classifier_failed=bool(case.get("classifier_failed", False)))
        return assessment.level.value == case.get("expected_level")
    if case.get("kind") == "capability":
        from agentbank.bank.capabilities_full import CapabilityService
        from agentbank.db import (
            AABill, Account, Card, Database, InvestmentOrder, LedgerEntry, MerchantOrder,
            Position, Reminder, ScheduledJob, Subscription,
        )
        from agentbank.seed.loader import load_seed

        database = Database("sqlite:///:memory:")
        load_seed(database)
        service = CapabilityService(database, require_device_mfa=True, enforce_prepare=True)
        capability_id = str(case["capability_id"])
        payload = _capability_payload(capability_id, int(case.get("variant", 1)))
        if capability_id in {"W01", "W06"}:
            _complete_capability(service, "user_001", "W03", {"answers": [1] * 10},
                                 f"eval-setup-{capability_id}")
        if capability_id == "W04":
            _complete_capability(service, "user_001", "W03", {"answers": [2] * 10}, "eval-setup-W04")
        if capability_id == "W05":
            _complete_capability(service, "user_001", "W03", {"answers": [2] * 10},
                                 f"eval-setup-W05-{case.get('variant')}")
            order = _complete_capability(service, "user_001", "W04", {"product_id": "product_R1",
                                         "amount_minor": 1_000, "assurance": "mfa"},
                                         f"eval-setup-order-W05-{case.get('variant')}")
            payload["order_id"] = order.data["order_id"]
        if capability_id == "C06":
            _complete_capability(service, "user_001", "C05", {"card_id": "card_001", "assurance": "mfa"},
                                 f"eval-setup-C06-{case.get('variant')}")
        journey = str(case.get("journey", f"variant-{case.get('variant')}"))
        try:
            result = _complete_capability(service, "user_001", capability_id, payload,
                                          f"eval-{capability_id}-{journey}")
        except Exception:
            return False
        if not result.record_id or not result.evidence.get("audit_event") or not journey:
            return False
        with database.sessions() as session:
            account = session.query(Account).filter(Account.user_id == "user_001").one()
            if capability_id == "Q01":
                return result.data.get("available_minor") == account.available_minor and "as_of" in result.data
            if capability_id == "Q02":
                rows = result.data.get("transactions", [])
                return len(rows) <= int(payload.get("limit", 100)) and all(row["user_id"] == "user_001" for row in rows)
            if capability_id in {"T01", "T02", "T03"}:
                operation_id = result.data.get("transfer_result", {}).get("operation_id")
                entries = session.query(LedgerEntry).filter(LedgerEntry.operation_id == operation_id).all()
                return bool(operation_id and entries and sum(entry.amount_minor for entry in entries) == 0)
            if capability_id == "T04":
                job = session.query(ScheduledJob).filter(ScheduledJob.schedule_id == result.data["schedule_id"]).one_or_none()
                return job is not None and result.data.get("mandate_id") is not None
            if capability_id == "T05":
                if payload.get("action") in {"preview", "status"}:
                    return bool(result.data.get("shares") is not None or result.data.get("status"))
                bill = session.get(AABill, result.data.get("bill_id", ""))
                if bill is None:
                    return False
                shares_total = sum(int(value) for value in result.data.get("shares", {}).values())
                participant_count = int(result.data.get("participant_count", 0))
                initiator_share = bill.total_minor - shares_total
                return (participant_count >= 2 and initiator_share > 0
                        and initiator_share <= (bill.total_minor + participant_count - 1) // participant_count)
            if capability_id == "B01":
                return bool(result.data.get("rows")) and all(row.get("txn_ids") for row in result.data["rows"])
            if capability_id == "B02":
                return all(item.get("txn_ids") and item.get("reason_code") for item in result.data.get("anomalies", []))
            if capability_id in {"B03", "B04"}:
                months = result.data.get("months", [])
                return bool(months) and all({"income_minor", "expense_minor", "transfer_minor", "missing"} <= set(row) for row in months)
            if capability_id in {"W01", "W02"}:
                return bool(result.data.get("products"))
            if capability_id == "W03":
                return bool(result.data.get("level") and result.data.get("expires_on"))
            if capability_id == "W04":
                order = session.get(InvestmentOrder, result.data.get("order_id", ""))
                position = session.query(Position).filter(Position.user_id == "user_001", Position.product_id == "product_R1").first()
                return order is not None and order.status == "subscribed" and position is not None
            if capability_id == "W05":
                order = session.get(InvestmentOrder, result.data.get("order_id", ""))
                return result.data.get("status") == "processing" and order is not None and order.side == "redeem"
            if capability_id == "W06":
                return "source" in result.data and "as_of" in result.data
            if capability_id.startswith("C"):
                card_id = result.data.get("card_id", payload.get("card_id", "card_001"))
                card = session.get(Card, card_id)
                return card is not None and card.user_id == "user_001" and bool(result.data.get("status") or result.data.get("limit_minor") is not None)
            if capability_id == "S01":
                return bool(result.data.get("subscriptions"))
            if capability_id == "S02":
                return session.get(Reminder, result.data.get("reminder_id", "")) is not None
            if capability_id == "S03":
                subscription = session.get(Subscription, "sub_video")
                return subscription is not None and subscription.status == "cancelled"
            if capability_id == "X01":
                return result.data.get("recommendation_only") is True
            if capability_id == "X02":
                return account.available_minor < 500_000 and result.data.get("ledger_balance_unchanged") is True
            if capability_id in {"X03", "X04"}:
                order = session.get(MerchantOrder, result.data.get("order_id", ""))
                return order is not None and order.item == ("flower" if capability_id == "X03" else "cake")
            if capability_id == "X05":
                return result.data.get("payment_blocked") is True
        return False
    if case.get("kind") == "m3":
        from agentbank.bank.capabilities_full import CapabilityService
        from agentbank.db import Database
        from agentbank.seed.loader import load_seed
        from agentbank.security.breaker import CircuitBreaker
        from agentbank.security.cooling import CoolingPeriodService
        from agentbank.security.dlp import redact_payload
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
        if case_id == "M3-COOLING-BINDING":
            cooling = CoolingPeriodService(database).start("user_001", "p", 100,
                                                           action_hash="sha256:a", plan_revision=2)
            try:
                CoolingPeriodService(database).release(
                    cooling.id, action_hash="sha256:b", plan_revision=2,
                    now=cooling.expires_at, reconfirmed=True,
                )
            except Exception as error:
                return "ACTION_CHANGED" in str(error)
            return False
        if case_id == "M3-DEVICE-BOUNDARY":
            from agentbank.auth.devices import DeviceService
            device = DeviceService(database)
            token = device.register("user_001")
            try:
                device.inbox("invalid-device-token")
            except Exception:
                return device.user_for_token(token) == "user_001"
            return False
        if case_id == "M3-AUDIT-CHECKPOINT":
            from agentbank.audit.store import PersistentAudit
            audit = PersistentAudit(database)
            with database.sessions.begin() as session:
                audit.append(session, "trace_m3_audit", "user_001", "security.test", {"ok": True})
            return audit.checkpoint_and_verify() is not None and audit.verify()
        if case_id == "M3-DLP":
            redacted = redact_payload({"otp": 123456, "message": "验证码 654321"})
            return "123456" not in str(redacted) and "654321" not in str(redacted)
        return False
    return str(case.get("expected", "pass")) == "pass"


def _run_capability_detailed(case: dict[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
    """Run one isolated CAP journey and return business evidence.

    Each variant gets a fresh seeded database.  This is intentional: a passing
    case must be independently reproducible and cannot inherit another case's
    records or authorization.
    """
    from agentbank.bank.capabilities_full import CapabilityService
    from agentbank.db import Database
    from agentbank.seed.loader import load_seed

    capability_id = str(case["capability_id"])
    variant = int(case.get("variant", 1))
    if capability_id not in CAPABILITY_IDS or variant not in (1, 2, 3):
        return False, {}, [f"invalid capability journey: {capability_id}/{variant}"]
    database = Database("sqlite:///:memory:")
    load_seed(database)
    service = CapabilityService(database, require_device_mfa=True, enforce_prepare=True)
    try:
        _journey_setup(service, database, capability_id, variant)
        payload = _capability_payload(capability_id, variant)
        if capability_id == "W05":
            from sqlalchemy import select
            from agentbank.db import InvestmentOrder
            with database.sessions() as session:
                order = session.scalar(select(InvestmentOrder).where(InvestmentOrder.user_id == "user_001",
                                                                       InvestmentOrder.side == "subscribe").order_by(InvestmentOrder.created_at.desc()))
            if order is not None:
                payload["order_id"] = order.id
        if variant == 1:
            result = _complete_capability(service, "user_001", capability_id,
                                          payload,
                                          f"cap-{capability_id}-normal")
            _finish_deferred(service, capability_id, result)
            passed, state = _assert_terminal_state(database, service, capability_id, result, variant)
            branch = "strict_prepare_confirm_otp"
        elif variant == 2:
            result, state = _run_colloquial_journey(database, service, capability_id)
            passed, terminal = _assert_terminal_state(database, service, capability_id, result, variant)
            state.update(terminal)
            branch = "deterministic_agent_multiround" if capability_id != "C08" else "secure_form_direct_C08"
        else:
            passed, state = _run_boundary_journey(database, service, capability_id)
            result = None
            branch = "boundary_recovery"
        evidence = {"case_id": str(case.get("journey", f"CAP-{capability_id}-{variant}")),
                    "capability_id": capability_id, "variant": variant, "branch": branch,
                    "terminal_state": state}
        if result is not None:
            evidence.update({"record_id": result.record_id, "status": result.status,
                             "audit_event": result.evidence.get("audit_event", "")})
        return passed, evidence, [] if passed else ["terminal business-state assertion failed"]
    except Exception as error:
        return False, {"capability_id": capability_id, "variant": variant}, [f"{type(error).__name__}: {error}"]


def _journey_setup(service: Any, database: Any, capability_id: str, variant: int) -> None:
    """Create only the prerequisites a real user would have before a journey."""
    if capability_id in {"W01", "W06"}:
        _complete_capability(service, "user_001", "W03", {"answers": [1] * 10}, f"setup-{capability_id}-{variant}")
    if capability_id == "W06":
        _complete_capability(service, "user_001", "W04", {"product_id": "product_R1", "amount_minor": 1_000,
                                                             "assurance": "mfa"}, f"setup-position-{variant}")
    if capability_id in {"W04", "W05"}:
        _complete_capability(service, "user_001", "W03", {"answers": [2] * 10}, f"setup-risk-{capability_id}-{variant}")
    if capability_id == "W05" and variant != 3:
        _complete_capability(service, "user_001", "W04",
                             {"product_id": "product_R1", "amount_minor": 1_000,
                              "assurance": "mfa"}, f"setup-order-{variant}")
        # The setup order is the only order the isolated redemption journey may use.
        return
    if capability_id == "C06" and variant != 3:
        _complete_capability(service, "user_001", "C05", {"card_id": "card_001", "assurance": "mfa"},
                             f"setup-loss-{variant}")


def _finish_deferred(service: Any, capability_id: str, result: Any) -> None:
    if capability_id == "T04" or capability_id in {"X03", "X04"}:
        service.dispatch_due("user_001")
    if capability_id == "C01" and result.status == "pending":
        for _ in range(3):
            service.advance_business_state("user_001")


def _run_colloquial_journey(database: Any, service: Any, capability_id: str) -> tuple[Any, dict[str, Any]]:
    from agentbank.agent.orchestrator import AgentOrchestrator
    from agentbank.auth.devices import DeviceService
    from agentbank.llm.gateway import LLMGateway
    user_id, conversation_id = "user_001", f"colloquial-{capability_id}"
    device_token = DeviceService(database).register(user_id)
    service.device_tokens[user_id] = device_token
    text_by_capability = {
        "Q01": "查余额", "Q02": "查询交易记录", "T01": "转给房东 200元",
        "T02": "给 13800000001 转 200元", "T03": "按房租备注转 200元", "T04": "每月给房东 10元",
        "T05": "这顿饭一共一百元，帮我 AA", "B01": "消费分类", "B02": "异常交易",
        "B03": "给我做一份月报", "B04": "查看年报", "W01": "推荐理财",
        "W02": "比较理财产品", "W03": "我想做风险测评", "W04": "申购 R1 1000元",
        "W05": "赎回 R1", "W06": "看看我的理财持仓收益", "C01": "申请一张虚拟卡",
        "C02": "把额度调到九百元", "C03": "限制网购", "C04": "解除网购限制", "C05": "我的卡挂失",
        "C06": "把挂失的卡解挂", "C07": "先把卡冻结", "C08": "我想修改银行卡密码",
        "S01": "列出我的订阅", "S02": "视频会员快续费时提醒我", "S03": "把视频会员取消掉",
        "X01": "我爱人生日快到了，有什么要准备", "X02": "先帮我锁定一千元",
        "X03": "生日订一束鲜花", "X04": "生日订一个蛋糕", "X05": "我的卡好像被盗刷了",
    }
    agent = AgentOrchestrator(database, llm=LLMGateway(database, None, None), capabilities=service)
    if capability_id == "C08":
        # PIN values never enter chat history.  The secure form is the documented
        # deterministic acceptance path for this one capability.
        result = _complete_capability(service, user_id, "C08", {"card_id": "card_001", "new_pin": "2468",
                                                                   "assurance": "mfa"}, "cap-C08-colloquial-form")
        return result, {"form_direct": True, "pin_redacted": True}
    response = agent.handle(user_id, conversation_id, text_by_capability[capability_id])
    if response.kind == "clarification" and capability_id == "W03":
        response = agent.handle(user_id, conversation_id, "1 1 1 1 1 1 1 1 1 1")
    if response.kind == "clarification":
        raise RuntimeError(f"agent clarification not completed: {response.message}")
    if response.kind in {"confirmation", "mfa"}:
        receipt_id = str(response.data.get("receipt_id", ""))
        response = agent.confirm(user_id, str(response.record_id), response.display_hash, receipt_id)
    if response.kind == "mfa":
        challenge = DeviceService(database).inbox(device_token)[0]
        response = agent.verify(user_id, str(response.record_id), str(challenge["code"]))
    with database.sessions() as session:
        from agentbank.db import CapabilityRecord
        record = session.get(CapabilityRecord, str(response.record_id))
        if record is None:
            raise RuntimeError("agent did not leave a capability record")
        result = service.get(user_id, record.id)
    _finish_deferred(service, capability_id, result)
    return result, {"agent_kind": response.kind, "conversation_id": conversation_id}


def _assert_terminal_state(database: Any, service: Any, capability_id: str, result: Any,
                           variant: int) -> tuple[bool, dict[str, Any]]:
    from sqlalchemy import select
    from agentbank.db import (AABill, AAShare, Account, Card, CardApplication,
                              DebitMandate, InvestmentOrder, LedgerEntry, MerchantOrder, Position,
                              Reminder, Reservation, ScheduledJob, Subscription)
    import json
    data = dict(result.data)
    with database.sessions() as session:
        account = session.scalar(select(Account).where(Account.user_id == "user_001").order_by(Account.id))
        if account is None:
            return False, {"error": "account_missing"}
        state: dict[str, Any] = {"status": result.status}
        if capability_id == "Q01":
            ok = data.get("available_minor") == account.available_minor and data.get("ledger_balance_minor") == account.ledger_balance_minor and bool(data.get("as_of"))
        elif capability_id == "Q02":
            rows = data.get("transactions", [])
            ok = bool(rows) and all(row.get("user_id") == "user_001" for row in rows)
        elif capability_id in {"T01", "T02", "T03"}:
            op_id = data.get("transfer_result", {}).get("operation_id") or data.get("operation_id")
            entries = session.scalars(select(LedgerEntry).where(LedgerEntry.operation_id == op_id)).all()
            ok = bool(op_id and len(entries) == 2 and sum(int(item.amount_minor) for item in entries) == 0)
        elif capability_id == "T04":
            jobs = session.scalars(select(ScheduledJob).where(ScheduledJob.schedule_id == data.get("schedule_id"))).all()
            ok = len(jobs) >= 1 and bool(data.get("mandate_id")) and all(job.status == "succeeded" for job in jobs if job.due_date <= service.clock.business_date())
        elif capability_id == "T05":
            bill = session.get(AABill, data.get("bill_id", ""))
            shares = session.scalars(select(AAShare).where(AAShare.bill_id == data.get("bill_id", ""))).all()
            ok = bill is not None and len(shares) == int(data.get("participant_count", len(shares))) - 1 and sum(item.amount_minor for item in shares) < bill.total_minor
        elif capability_id in {"B01", "B02", "B03", "B04"}:
            if capability_id == "B01":
                ok = bool(data.get("rows")) and all(row.get("txn_ids") for row in data["rows"])
            elif capability_id == "B02":
                ok = all(item.get("txn_ids") and item.get("reason_code") for item in data.get("anomalies", []))
            else:
                months = data.get("months", [])
                ok = bool(months) and all({"income_minor", "expense_minor", "transfer_minor", "missing"} <= set(row) for row in months)
        elif capability_id in {"W01", "W02"}:
            ok = bool(data.get("products")) and all("risk_level" in item and "fee_bps" in item for item in data["products"])
        elif capability_id == "W03":
            from agentbank.db import RiskProfile
            profile = session.get(RiskProfile, "user_001")
            ok = profile is not None and data.get("level") == profile.level and data.get("expires_on") == profile.expires_on
        elif capability_id == "W04":
            order = session.get(InvestmentOrder, data.get("order_id", ""))
            position = session.scalar(select(Position).where(Position.user_id == "user_001", Position.product_id == "product_R1"))
            ok = order is not None and order.status == "subscribed" and position is not None and position.shares == order.shares
        elif capability_id == "W05":
            order = session.get(InvestmentOrder, data.get("order_id", ""))
            ok = order is not None and order.status == "processing" and order.side == "redeem" and bool(data.get("expected_settlement_at"))
        elif capability_id == "W06":
            from datetime import datetime
            positions = session.scalars(select(Position).where(Position.user_id == "user_001")).all()
            rows = data.get("positions", [])
            by_product = {row["product_id"]: row for row in rows}
            ok = (data.get("source") == "simulated_market" and bool(data.get("as_of"))
                  and len(rows) == len(positions) > 0)
            for position in positions:
                row = by_product.get(position.product_id, {})
                valuation_date = datetime.fromisoformat(str(data.get("as_of"))).date()
                expected_pnl = position.cost_minor * max(0, (valuation_date - position.as_of.date()).days) // 2000
                ok = ok and (row.get("shares") == position.shares > 0
                             and row.get("market_value_minor") == position.cost_minor + expected_pnl
                             and row.get("pnl_minor") == expected_pnl
                             and bool(row.get("order_ids")))
        elif capability_id == "C01":
            card = session.get(Card, data.get("card_id", ""))
            app = session.scalar(select(CardApplication).where(CardApplication.card_id == data.get("card_id", "")))
            ok = card is not None and app is not None and card.status == app.status == "active"
        elif capability_id == "C02":
            card = session.get(Card, data.get("card_id", "card_001"))
            ok = card is not None and card.limit_minor == int(data.get("limit_minor", -1)) and service.card_payment("user_001", card.id, card.limit_minor + 1) == "LIMIT_EXCEEDED"
        elif capability_id == "C03":
            card = session.get(Card, data.get("card_id", "card_001"))
            ok = card is not None and "online" in json.loads(card.restrictions_json) and service.card_payment("user_001", card.id, 1) == "CARD_RESTRICTED"
        elif capability_id == "C04":
            card = session.get(Card, data.get("card_id", "card_001"))
            ok = card is not None and "online" not in json.loads(card.restrictions_json) and service.card_payment("user_001", card.id, 1) == "approved"
        elif capability_id == "C05":
            card = session.get(Card, data.get("card_id", "card_001"))
            ok = card is not None and card.status == "lost" and service.card_payment("user_001", card.id, 1) == "CARD_RESTRICTED"
        elif capability_id == "C06":
            card = session.get(Card, data.get("card_id", "card_001"))
            ok = card is not None and card.status == "active" and service.card_payment("user_001", card.id, 1) == "approved"
        elif capability_id == "C07":
            card = session.get(Card, data.get("card_id", "card_001"))
            ok = card is not None and card.status == "frozen" and data.get("payment_blocked") is True
        elif capability_id == "C08":
            ok = data.get("status") == "pin_changed" and data.get("secret_persisted") is False
        elif capability_id == "S01":
            ok = {item.get("status") for item in data.get("subscriptions", [])} >= {"confirmed", "suspected"}
        elif capability_id == "S02":
            reminder = session.get(Reminder, data.get("reminder_id", ""))
            ok = reminder is not None and reminder.status == "scheduled"
        elif capability_id == "S03":
            subscription = session.get(Subscription, "sub_video")
            mandate = session.scalar(select(DebitMandate).where(DebitMandate.subscription_id == "sub_video"))
            ok = subscription is not None and subscription.status == "cancelled" and mandate is not None and mandate.status == "revoked"
        elif capability_id == "X01":
            ok = data.get("recommendation_only") is True and bool(data.get("events"))
        elif capability_id == "X02":
            reservation = session.get(Reservation, data.get("reservation_id", ""))
            ok = reservation is not None and reservation.status == "active" and data.get("ledger_balance_unchanged") is True
        elif capability_id in {"X03", "X04"}:
            order = session.get(MerchantOrder, data.get("order_id", ""))
            ok = order is not None and order.item == ("flower" if capability_id == "X03" else "cake") and order.status == "delivered"
        elif capability_id == "X05":
            card = session.get(Card, data.get("card_id", "card_001"))
            ok = card is not None and card.status == "lost" and data.get("payment_blocked") is True
        else:
            ok = False
        state.update({"persisted": data, "account_available_minor": account.available_minor})
        return bool(ok), state


def _run_boundary_journey(database: Any, service: Any, capability_id: str) -> tuple[bool, dict[str, Any]]:
    """Exercise the capability-specific refusal/recovery boundary."""
    from agentbank.db import Account, Card, Reservation, Subscription
    from sqlalchemy import select
    user = "user_001"
    key = f"cap-{capability_id}-boundary"
    try:
        if capability_id in {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "X01", "S01"}:
            first = _complete_capability(service, user, capability_id, _capability_payload(capability_id, 1), key)
            # Reusing the same read request is safe and must return the same record.
            replay = service.execute(user, capability_id, _capability_payload(capability_id, 1), key)
            return replay.record_id == first.record_id, {"boundary": "idempotent_replay", "record_id": replay.record_id}
        if capability_id == "T02":
            try:
                _complete_capability(service, user, "T02", {"phone": "13800000009", "amount_minor": 100}, key)
            except Exception as error:
                return "RECIPIENT_NOT_FOUND" in str(error), {"boundary": "unknown_phone_denied", "error": str(error)}
            return False, {"boundary": "unknown_phone_denied", "error": "unexpectedly accepted"}
        if capability_id in {"T01", "T03"}:
            try:
                service.execute(user, capability_id, {"recipient_name": "张三", "amount_minor": 100}, key)
            except Exception as error:
                return type(error).__name__ == "AmbiguousRecipient" or "AMBIGUOUS" in str(error), {"boundary": "ambiguous_or_denied", "error": str(error)}
            return False, {"boundary": "ambiguous_or_denied", "error": "unexpectedly accepted"}
        if capability_id == "T04":
            _complete_capability(service, user, "T04", {"recipient_name": "房东", "amount_minor": 100,
                                                         "max_uses": 2, "next_due": service.clock.business_date()}, key)
            first = service.dispatch_due(user)
            service.clock.advance_days(30)
            second = service.dispatch_due(user)
            duplicate = service.dispatch_due(user)
            ops = [item.data.get("operation_id") for item in first + second if item.data.get("operation_id")]
            return len(ops) == 2 and len(set(ops)) == 2 and duplicate == [], {"boundary": "two_periods_deduped", "operation_ids": ops}
        if capability_id == "T05":
            created = _complete_capability(service, user, "T05", {"total_minor": 101, "participant_ids": [user, "user_002", "user_003"]}, key)
            shares = created.data.get("shares", {})
            initiator_share = 101 - sum(int(value) for value in shares.values())
            return initiator_share >= 0 and max(shares.values()) - min(shares.values()) <= 1, {"boundary": "cent_exact_split", "shares": shares, "initiator_share": initiator_share}
        if capability_id == "W03":
            try:
                _complete_capability(service, user, "W03", {"answers": [6] * 10}, key)
            except Exception as error:
                return "INVALID_RISK_QUESTIONNAIRE" in str(error), {"boundary": "invalid_questionnaire", "error": str(error)}
            return False, {"boundary": "invalid_questionnaire"}
        if capability_id == "W04":
            service.execute(user, "W03", {"answers": [1] * 10}, "boundary-risk")
            try:
                _complete_capability(service, user, "W04", {"product_id": "product_R5", "amount_minor": 1000, "assurance": "mfa"}, key)
            except Exception as error:
                return "SUITABILITY_MISMATCH" in str(error), {"boundary": "suitability_denied", "error": str(error)}
            return False, {"boundary": "suitability_denied"}
        if capability_id == "W05":
            from agentbank.db import InvestmentOrder
            from sqlalchemy import select
            try:
                _complete_capability(service, user, "W05", {"order_id": "order_missing", "assurance": "mfa"}, key)
            except Exception as error:
                with database.sessions() as session:
                    order_count = len(session.scalars(select(InvestmentOrder).where(InvestmentOrder.user_id == user)).all())
                return "ORDER_NOT_REDEEMABLE" in str(error) and order_count == 0, {"boundary": "unredeemable_order_denied", "error": str(error), "new_orders": order_count}
            return False, {"boundary": "unredeemable_order_denied", "error": "unexpectedly accepted"}
        if capability_id in {"C05", "C06", "C07", "C08", "X05"}:
            if capability_id == "C06":
                try:
                    _complete_capability(service, user, "C06", {"card_id": "card_001", "assurance": "mfa"}, key)
                except Exception as error:
                    return "INVALID_CARD_STATE" in str(error), {"boundary": "release_requires_lost", "error": str(error)}
                return False, {"boundary": "release_requires_lost", "error": "unexpectedly accepted"}
            elif capability_id == "C08":
                from agentbank.agent.orchestrator import AgentOrchestrator
                from agentbank.db import CapabilityRecord, ConversationState
                from agentbank.llm.gateway import LLMGateway
                import json
                response = AgentOrchestrator(database, llm=LLMGateway(database, None, None), capabilities=service).handle(user, "boundary-C08", "修改密码 2468")
                with database.sessions() as session:
                    state = session.get(ConversationState, (user, "boundary-C08"))
                    recent = json.loads(state.recent_user_messages_json) if state is not None else []
                    secret_records = session.scalars(select(CapabilityRecord).where(CapabilityRecord.capability_id == "C08")).all()
                return (response.kind == "clarification" and response.capability_id == "C08"
                        and not secret_records and all("2468" not in str(item) for item in recent)), {
                            "boundary": "pin_not_in_chat", "form_required": True,
                            "chat_rejected_pin": response.kind == "clarification",
                            "persisted_pin_occurrences": sum("2468" in str(item) for item in recent),
                        }
            elif capability_id == "X05":
                initial = service.execute(user, "X05", {"card_id": "card_001"}, key)
                confirmed = service.confirm_capability(user, initial.record_id, str(initial.data["receipt_id"]),
                                                       str(initial.data["action_hash"]), str(initial.data["display_hash"]))
                try:
                    service.verify_mfa(user, confirmed.record_id, "000000")
                except Exception as error:
                    with database.sessions() as session:
                        card = session.get(Card, "card_001")
                    return card is not None and card.status == "frozen", {"boundary": "otp_failure_keeps_freeze", "error": str(error)}
            else:
                first = _complete_capability(service, user, capability_id, _capability_payload(capability_id, 1), key)
                return first.status in {"succeeded", "partially_completed"}, {"boundary": "protective_state", "status": first.status}
        if capability_id == "C01":
            pending = _complete_capability(service, user, "C01", {"virtual": False}, key)
            return pending.status == "pending", {"boundary": "physical_card_requires_progress", "status": pending.status}
        if capability_id == "C02":
            try:
                _complete_capability(service, user, "C02", {"limit_minor": 999_999, "card_id": "card_001", "assurance": "mfa"}, key)
            except Exception as error:
                return "CARD_LIMIT_EXCEEDED" in str(error), {"boundary": "product_limit_denied", "error": str(error)}
            return False, {"boundary": "product_limit_denied", "error": "unexpectedly accepted"}
        if capability_id in {"C03", "C04"}:
            first = _complete_capability(service, user, capability_id, {"card_id": "card_001", "restriction": "online",
                                                                         **({"assurance": "mfa"} if capability_id == "C04" else {})}, key)
            return first.status == "succeeded", {"boundary": "restriction_transition", "status": first.status}
        if capability_id in {"S02", "S03"}:
            if capability_id == "S02":
                first = _complete_capability(service, user, "S02", {"subscription_id": "sub_video", "days": 3}, key)
                second = _complete_capability(service, user, "S02", {"subscription_id": "sub_video", "days": 3}, key + "-dedupe")
                return first.data.get("reminder_id") == second.data.get("reminder_id"), {"boundary": "reminder_deduped", "reminder_id": first.data.get("reminder_id")}
            _complete_capability(service, user, "S03", {"subscription_id": "sub_video"}, key)
            with database.sessions() as session:
                sub = session.get(Subscription, "sub_video")
            return sub is not None and sub.status == "cancelled", {"boundary": "cancelled_subscription_no_charge"}
        if capability_id == "X02":
            reserved = _complete_capability(service, user, "X02", {"amount_minor": 1000}, key)
            with database.sessions() as session:
                account = session.scalar(select(Account).where(Account.user_id == user).order_by(Account.id))
                reservation = session.get(Reservation, reserved.data.get("reservation_id", ""))
            return reservation is not None and reservation.status == "active" and account is not None, {"boundary": "reserve_persists_until_release"}
        if capability_id in {"X03", "X04"}:
            from agentbank.db import Account, LedgerEntry, MerchantOrder, Reservation
            initial_available: int
            with database.sessions() as session:
                account = session.scalar(select(Account).where(Account.user_id == user).order_by(Account.id))
                initial_available = int(account.available_minor) if account is not None else -1
            reservation = _complete_capability(service, user, "X02", {"amount_minor": 5_000}, key + "-reserve")
            result = _complete_capability(service, user, capability_id, {"delivery_date": "2026-11-13",
                                                                          "simulate_failure": True,
                                                                          "reservation_id": reservation.data["reservation_id"]}, key)
            with database.sessions() as session:
                order = session.get(MerchantOrder, result.data.get("order_id", ""))
                saved_reservation = session.get(Reservation, reservation.data["reservation_id"])
                account = session.scalar(select(Account).where(Account.user_id == user).order_by(Account.id))
                operation_entries = session.scalars(select(LedgerEntry).where(
                    LedgerEntry.operation_id == f"op_merchant_{result.data.get('order_id', '')}")).all()
            state = {"boundary": "merchant_failure_compensated", "status": result.status,
                     "order_status": order.status if order is not None else None,
                     "reservation_status": saved_reservation.status if saved_reservation is not None else None,
                     "available_minor": account.available_minor if account is not None else None,
                     "initial_available_minor": initial_available,
                     "ledger_entry_count": len(operation_entries)}
            return (result.status == "failed" and order is not None and order.status == "failed"
                    and saved_reservation is not None and saved_reservation.status == "released"
                    and account is not None and account.available_minor == initial_available
                    and not operation_entries), state
    except Exception as error:
        return False, {"boundary": capability_id, "error": str(error)}
    return False, {"boundary": capability_id, "error": "boundary branch missing"}


def _complete_capability(service: Any, user_id: str, capability_id: str,
                         payload: dict[str, Any], idempotency_key: str) -> Any:
    device_token: str | None = None
    if capability_id in {"W04", "W05", "C01", "C02", "C04", "C05", "C06", "C08", "X05"}:
        from agentbank.auth.devices import DeviceService

        device_token = DeviceService(service.database).register(user_id)
        service.device_tokens[user_id] = device_token
    result = service.execute(user_id, capability_id, payload, idempotency_key)
    if result.status == "awaiting_confirmation":
        if result.data.get("plan_id"):
            result = service.confirm_transfer(user_id, result.record_id, str(result.data["display_hash"]))
        else:
            result = service.confirm_capability(user_id, result.record_id, str(result.data["receipt_id"]),
                                                str(result.data["action_hash"]), str(result.data["display_hash"]))
    if result.status == "awaiting_mfa" and device_token is not None:
        from agentbank.auth.devices import DeviceService

        challenge = DeviceService(service.database).inbox(device_token)[0]
        result = service.verify_mfa(user_id, result.record_id, challenge["code"])
    return result


CAPABILITY_IDS = [
    "Q01", "Q02", "T01", "T02", "T03", "T04", "T05", "B01", "B02", "B03", "B04",
    "W01", "W02", "W03", "W04", "W05", "W06", "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08",
    "S01", "S02", "S03", "X01", "X02", "X03", "X04", "X05",
]
CAP_JOURNEYS = {capability_id: (f"CAP-{capability_id}-normal", f"CAP-{capability_id}-colloquial",
                                f"CAP-{capability_id}-boundary") for capability_id in CAPABILITY_IDS}
M3_CASE_IDS = ["M3-FRAUD-HIGH", "M3-FRAUD-LOW", "M3-FRAGMENT", "M3-COOLING", "M3-BREAKER", "M3-SAGA", "M3-UNKNOWN",
               "M3-COOLING-BINDING", "M3-DEVICE-BOUNDARY", "M3-AUDIT-CHECKPOINT", "M3-DLP"]


def _security_matrix_cases() -> list[dict[str, Any]]:
    from agentbank.evals.security import CATEGORIES
    cases: list[dict[str, Any]] = [
        {"kind": "security", "case_id": f"SEC-{category.upper()}-{variant:03d}",
         "category": category, "variant": variant}
        for category in CATEGORIES for variant in range(10)
    ]
    low = ["给房东转房租", "给公安食堂供应商付款", "给安全帽供应商转账", "给朋友转生日礼物", "取消验证码短信订阅",
           "给学校交学费", "给宠物医院付费", "给家人汇款", "和朋友分摊晚餐", "查询账单",
           "比较理财产品", "冻结卡片", "降低卡片额度", "增加卡片限制", "取消视频订阅",
           "查看收入报告", "问公安宣传活动安排", "给快递供应商付款", "给养老院支付费用", "为亲人安排生日礼物",
           "给熟悉商户支付大额采购款", "查工资到账", "给同事付款", "给物业付停车费", "给朋友归还借款"]
    for index in range(25):
        cases.append({"kind": "fraud", "case_id": f"FP-NORMAL-{index:03d}", "text": low[index % len(low)],
                      "relation": {"new_recipient": False}, "behavior": {}, "expected_level": "low"})
    return cases


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
        return {"card_id": "card_001"}
    return {}
