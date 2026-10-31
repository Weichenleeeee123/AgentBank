from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentbank.audit.store import PersistentAudit
from agentbank.auth.devices import DeviceService, _hash
from agentbank.clock import business_date, utc_now_naive
from agentbank.contracts import Assurance
from agentbank.security.fraud import FraudDetector, FraudLevel, record_risk
from agentbank.security.cooling import CoolingPeriodService
from agentbank.security.breaker import CircuitBreaker
from agentbank.workflow.recovery import HandoffService
from agentbank.db import (
    Account,
    Challenge,
    DailyRiskBucket,
    DecisionRecord,
    LedgerEntry,
    Operation,
    Payee,
    PlanNodeRecord,
    PlanRecord,
    ReceiptConsumption,
    ReceiptRecord,
    Database,
    CoolingPeriod,
    RiskContextRecord,
)
from agentbank.errors import AmbiguousRecipient, DomainError
from agentbank.policy.engine import PolicyContext, action_hash, decide


def _display_hash(display: list[dict[str, str]]) -> str:
    canonical = json.dumps(display, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PreparedTransfer:
    plan_id: str
    receipt_id: str
    display_hash: str
    action_hash: str
    tier_display: str
    assurance: Assurance
    display: list[dict[str, str]]


@dataclass(frozen=True)
class TransferResult:
    operation_id: str
    status: str
    amount_minor: int
    ledger_entry_ids: list[str]
    error_code: str | None = None


class TransferService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.audit = PersistentAudit(database)
        self.devices = DeviceService(database)
        self.fraud = FraudDetector()

    def balance(self, user_id: str) -> Account:
        with self.database.sessions.begin() as session:
            account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
            if account is None:
                raise DomainError("NOT_OWNER")
            self.audit.append(session, f"trace_balance_{uuid.uuid4().hex}", user_id, "account.balance.read",
                              {"account_id": account.id})
            return Account(id=account.id, user_id=account.user_id, available_minor=account.available_minor,
                           ledger_balance_minor=account.ledger_balance_minor, status=account.status)

    def prepare(self, user_id: str, recipient_name: str, amount_minor: int, memo: str | None = None) -> PreparedTransfer:
        if amount_minor <= 0:
            raise DomainError("INVALID_AMOUNT")
        with self.database.sessions.begin() as session:
            payees = session.scalars(select(Payee).where(Payee.owner_user_id == user_id,
                                                         Payee.display_name == recipient_name)).all()
            if not payees:
                raise DomainError("RECIPIENT_NOT_FOUND")
            if len(payees) > 1:
                raise AmbiguousRecipient()
            return self._prepare_selected(session, user_id, payees[0], amount_minor, memo)

    def prepare_for_payee(self, user_id: str, payee_id: str, amount_minor: int, memo: str | None = None) -> PreparedTransfer:
        if amount_minor <= 0:
            raise DomainError("INVALID_AMOUNT")
        with self.database.sessions.begin() as session:
            payee = session.get(Payee, payee_id)
            if payee is None or payee.owner_user_id != user_id:
                raise DomainError("NOT_OWNER")
            return self._prepare_selected(session, user_id, payee, amount_minor, memo)

    def _prepare_selected(self, session: Session, user_id: str, payee: Payee, amount_minor: int,
                          memo: str | None) -> PreparedTransfer:
            account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
            if account is None:
                raise DomainError("NOT_OWNER")
            bucket = session.get(DailyRiskBucket, (user_id, business_date()))
            completed = bucket.completed_minor if bucket else 0
            pending = bucket.pending_minor if bucket else 0
            operation_id = f"op_{uuid.uuid4().hex}"
            args = {"source_account_id": account.id, "recipient_id": payee.id, "amount_minor": amount_minor,
                    "currency": "CNY", "memo": memo}
            decision = decide("transfer.create", PolicyContext(projected_today_minor=completed + pending + amount_minor,
                                                                new_recipient=payee.is_new), operation_id=operation_id)
            if decision.executability != "allow":
                raise DomainError(decision.reason_codes[0])
            plan_id = f"plan_{uuid.uuid4().hex}"
            node_id = f"transfer_{uuid.uuid4().hex}"
            display = [{"label": "收款人", "value": payee.masked_name},
                       {"label": "金额", "value": f"¥{amount_minor / 100:.2f}"},
                       {"label": "币种", "value": "人民币"}]
            display_digest = _display_hash(display)
            digest = action_hash("transfer.create", args)
            session.add(PlanRecord(id=plan_id, user_id=user_id, revision=1, status="awaiting_confirmation" if decision.assurance is Assurance.confirm else "awaiting_mfa",
                                   display_hash=display_digest, action_hash=digest))
            session.add(PlanNodeRecord(id=node_id, plan_id=plan_id, tool="transfer.create", args_json=json.dumps(args, ensure_ascii=False),
                                       idempotency_key=f"idem_{plan_id}"))
            session.add(DecisionRecord(id=decision.decision_id, plan_id=plan_id, operation_id=operation_id,
                                       tier_display=decision.tier_display, assurance=decision.assurance.value,
                                       action_hash=digest, reason_codes_json=json.dumps(decision.reason_codes),
                                       facts_json=json.dumps(decision.facts)))
            receipt_id = f"rcp_{uuid.uuid4().hex}"
            session.add(ReceiptRecord(id=receipt_id, subject_id=user_id, plan_id=plan_id, plan_revision=1,
                                      node_ids_json=json.dumps([node_id]), action_hash=digest, display_hash=display_digest,
                                      assurance=decision.assurance.value, issued_at=utc_now_naive(),
                                      expires_at=utc_now_naive() + timedelta(minutes=5)))
            session.flush()
            if decision.assurance is Assurance.mfa:
                self._create_challenge(session, user_id, receipt_id, digest)
            self.audit.append(session, f"trace_{plan_id}", user_id, "policy.decided",
                              {"plan_id": plan_id, "tier": decision.tier_display, "reason_codes": decision.reason_codes})
            return PreparedTransfer(plan_id, receipt_id, display_digest, digest, decision.tier_display, decision.assurance, display)

    def _validate(self, session: Session, user_id: str, plan: PlanRecord,
                  receipt: ReceiptRecord, node: PlanNodeRecord, *, check_expiry: bool = True) -> dict[str, object]:
        CircuitBreaker(self.database).require_write(session, user_id)
        if plan.user_id != user_id or receipt.subject_id != user_id or receipt.plan_id != plan.id:
            raise DomainError("NOT_OWNER")
        if receipt.status != "pending":
            raise DomainError("RECEIPT_REPLAYED")
        if receipt.plan_revision != plan.revision:
            raise DomainError("PLAN_REVISION_MISMATCH")
        if plan.status in {"cancelled", "failed", "paused", "succeeded"}:
            raise DomainError("PLAN_STATE_INVALID")
        if node.id not in json.loads(receipt.node_ids_json):
            raise DomainError("RECEIPT_INVALID")
        if check_expiry and receipt.expires_at <= utc_now_naive():
            raise DomainError("RECEIPT_EXPIRED")
        args: dict[str, object] = json.loads(node.args_json)
        digest = action_hash(node.tool, args)
        if digest != receipt.action_hash or digest != plan.action_hash:
            raise DomainError("HASH_MISMATCH")
        payee = session.get(Payee, str(args.get("recipient_id", "")))
        if payee is None or payee.owner_user_id != user_id:
            raise DomainError("NOT_OWNER")
        display = [{"label": "收款人", "value": payee.masked_name},
                   {"label": "金额", "value": f"¥{int(str(args['amount_minor'])) / 100:.2f}"},
                   {"label": "币种", "value": "人民币"}]
        if _display_hash(display) != receipt.display_hash or receipt.display_hash != plan.display_hash:
            raise DomainError("HASH_MISMATCH")
        return args

    def _risk_gate(self, session: Session, user_id: str, plan: PlanRecord,
                   receipt: ReceiptRecord, node: PlanNodeRecord,
                   args: dict[str, object]) -> TransferResult | None:
        assessment = self.fraud.evaluate(str(args.get("memo") or ""), relation={}, behavior={})
        context = session.get(RiskContextRecord, user_id)
        level = assessment.level
        if context is not None and context.expires_at > utc_now_naive():
            if context.level == "high" or level != FraudLevel.high:
                level = FraudLevel(context.level)
        cooling = session.scalar(select(CoolingPeriod).where(CoolingPeriod.plan_id == plan.id,
                                                             CoolingPeriod.status == "active"))
        if level is FraudLevel.high:
            if cooling is not None:
                self._release_hold(session, cooling, args)
                cooling.status = "invalidated"
            plan.status = "failed"
            receipt.status = "invalidated"
            HandoffService(self.database).open(user_id, "transfer", "U07")
            self.audit.append(session, f"trace_{plan.id}", user_id, "security.fraud.blocked",
                              {"level": "high", "reason_codes": ["U07"]})
            return TransferResult("", "rejected", 0, [], "FRAUD_HIGH")
        if cooling is not None:
            if cooling.action_hash != plan.action_hash or cooling.plan_revision != plan.revision:
                raise DomainError("ACTION_CHANGED")
            if cooling.reconfirmed_at is not None and cooling.reconfirm_deadline is not None:
                if utc_now_naive() <= cooling.reconfirm_deadline and receipt.assurance == "mfa":
                    return None
            return TransferResult(cooling.id, "cooling_required", 0, [], "COOLING_PERIOD_REQUIRED")
        if level in {FraudLevel.medium, FraudLevel.unknown}:
            record_risk(self.database, user_id, level.value)
            account = session.get(Account, str(args["source_account_id"]))
            amount = int(str(args["amount_minor"]))
            if account is None or account.user_id != user_id or account.available_minor < amount:
                raise DomainError("INSUFFICIENT_FUNDS")
            view = CoolingPeriodService(self.database).start_in_session(
                session, user_id, plan.id, amount, action_hash=plan.action_hash, plan_revision=plan.revision)
            session.flush()
            cooling = session.get(CoolingPeriod, view.id)
            assert cooling is not None
            cooling.held = True
            cooling.business_day = business_date()
            account.available_minor -= amount
            self._bucket(session, user_id).pending_minor += amount
            plan.status = "paused"
            node.status = "cooling"
            self.audit.append(session, f"trace_{plan.id}", user_id, "security.cooling.started",
                              {"cooling_id": view.id, "action_hash": plan.action_hash,
                               "expires_at": view.expires_at.isoformat()})
            return TransferResult(view.id, "cooling_required", 0, [], "COOLING_PERIOD_REQUIRED")
        return None

    def confirm(self, user_id: str, plan_id: str, receipt_id: str, display_hash: str) -> TransferResult:
        with self.database.atomic() as session:
            plan = session.get(PlanRecord, plan_id)
            receipt = session.get(ReceiptRecord, receipt_id)
            node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id))
            if plan is None or receipt is None or node is None:
                raise DomainError("NOT_OWNER")
            if display_hash != receipt.display_hash:
                raise DomainError("HASH_MISMATCH")
            args = self._validate(session, user_id, plan, receipt, node)
            result = self._risk_gate(session, user_id, plan, receipt, node, args)
            if result is None:
                bucket = self._bucket(session, user_id)
                payee = session.get(Payee, str(args["recipient_id"]))
                needs_mfa = receipt.assurance == "mfa" or (payee is not None and payee.is_new) or (
                    bucket.completed_minor + bucket.pending_minor + int(str(args["amount_minor"])) > 100_000)
                if needs_mfa:
                    receipt.assurance = "mfa"
                    plan.status = "awaiting_mfa"
                    self._create_challenge(session, user_id, receipt.id, receipt.action_hash)
                    result = TransferResult("", "mfa_required", 0, [], "MFA_REQUIRED")
                else:
                    result = self._commit_transfer(session, user_id, plan, receipt, node, args)
        if result.error_code == "FRAUD_HIGH":
            raise DomainError("FRAUD_HIGH")
        if result.status == "succeeded":
            self.audit.checkpoint_and_verify()
        return result

    def verify(self, user_id: str, plan_id: str, code: str) -> TransferResult:
        with self.database.sessions() as session:
            plan = session.get(PlanRecord, plan_id)
            receipt = session.scalar(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan_id,
                                                                 ReceiptRecord.status == "pending").order_by(ReceiptRecord.issued_at.desc()))
            node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id))
            if receipt is None or plan is None or node is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            self._validate(session, user_id, plan, receipt, node)
            receipt_id, action_digest = receipt.id, receipt.action_hash
        self.devices.verify(user_id, code, receipt_id, action_digest)
        with self.database.atomic() as session:
            receipt = session.get(ReceiptRecord, receipt_id)
            plan = session.get(PlanRecord, plan_id)
            node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id))
            if receipt is None or plan is None or node is None:
                raise DomainError("NOT_OWNER")
            args = self._validate(session, user_id, plan, receipt, node)
            result = self._risk_gate(session, user_id, plan, receipt, node, args)
            if result is None:
                result = self._commit_transfer(session, user_id, plan, receipt, node, args)
        if result.error_code == "FRAUD_HIGH":
            raise DomainError("FRAUD_HIGH")
        if result.status == "succeeded":
            self.audit.checkpoint_and_verify()
        return result

    @staticmethod
    def _release_hold(session: Session, cooling: CoolingPeriod, args: dict[str, object]) -> None:
        if cooling.held:
            account = session.get(Account, str(args["source_account_id"]))
            if account is not None:
                account.available_minor += cooling.amount_minor
            bucket = session.get(DailyRiskBucket, (cooling.user_id, cooling.business_day))
            if bucket is not None:
                bucket.pending_minor -= cooling.amount_minor
            cooling.held = False

    def release_cooling(self, user_id: str, cooling_id: str, *, action_hash_value: str = "",
                        plan_revision: int | None = None,
                        now: datetime | None = None, reconfirmed: bool = False) -> TransferResult:
        current = _utc_aware(now or datetime.now(timezone.utc))
        with self.database.atomic() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None or cooling.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if cooling.status != "active":
                raise DomainError("COOLING_NOT_ACTIVE")
            if action_hash_value and action_hash_value != cooling.action_hash:
                raise DomainError("ACTION_CHANGED")
            if plan_revision is not None and plan_revision != cooling.plan_revision:
                raise DomainError("ACTION_CHANGED")
            if _utc_aware(cooling.expires_at) > current:
                raise DomainError("COOLING_PERIOD_ACTIVE")
            deadline = cooling.reconfirm_deadline or cooling.expires_at + timedelta(minutes=10)
            if current > _utc_aware(deadline) or not reconfirmed:
                raise DomainError("COOLING_RECONFIRMATION_REQUIRED")
            plan = session.get(PlanRecord, cooling.plan_id)
            receipt = session.scalar(select(ReceiptRecord).where(ReceiptRecord.plan_id == cooling.plan_id,
                                                                 ReceiptRecord.status == "pending"))
            node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == cooling.plan_id))
            if plan is None or receipt is None or node is None:
                raise DomainError("RECEIPT_INVALID")
            if plan.revision != cooling.plan_revision or plan.action_hash != cooling.action_hash:
                raise DomainError("ACTION_CHANGED")
            plan.status = "awaiting_mfa"
            args = self._validate(session, user_id, plan, receipt, node, check_expiry=False)
            result = self._risk_gate(session, user_id, plan, receipt, node, args)
            if result is None or result.error_code != "FRAUD_HIGH":
                # The old five-minute receipt cannot cover a thirty-minute wait.
                receipt.status = "invalidated"
                fresh = ReceiptRecord(id=f"rcp_{uuid.uuid4().hex}", subject_id=user_id, plan_id=plan.id,
                                      plan_revision=plan.revision, node_ids_json=receipt.node_ids_json,
                                      action_hash=plan.action_hash, display_hash=plan.display_hash,
                                      assurance="mfa", issued_at=current.replace(tzinfo=None),
                                      expires_at=min(current.replace(tzinfo=None)+timedelta(minutes=5), deadline))
                session.add(fresh)
                session.flush()
                cooling.reconfirmed_at = current.replace(tzinfo=None)
                self._create_challenge(session, user_id, fresh.id, fresh.action_hash)
                result = TransferResult(cooling.id, "mfa_required", cooling.amount_minor, [], "MFA_REQUIRED")
                self.audit.append(session, f"trace_{plan.id}", user_id, "security.cooling.reconfirmed",
                                  {"cooling_id": cooling.id, "receipt_id": fresh.id})
        if result.error_code == "FRAUD_HIGH":
            raise DomainError("FRAUD_HIGH")
        if result.status == "succeeded":
            self.audit.checkpoint_and_verify()
        return result

    def cancel_cooling(self, user_id: str, cooling_id: str) -> None:
        with self.database.atomic() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None or cooling.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if cooling.status != "active":
                raise DomainError("COOLING_NOT_ACTIVE")
            node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == cooling.plan_id))
            plan = session.get(PlanRecord, cooling.plan_id)
            if node is None or plan is None:
                raise DomainError("PLAN_STATE_INVALID")
            self._release_hold(session, cooling, json.loads(node.args_json))
            cooling.status = "cancelled"
            plan.status, node.status = "cancelled", "cancelled"
            for receipt in session.scalars(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan.id)).all():
                receipt.status = "invalidated"
            self.audit.append(session, f"trace_{plan.id}", user_id, "security.cooling.cancelled", {"cooling_id": cooling.id})

    def commit_scheduled(self, user_id: str, recipient_id: str, amount_minor: int, plan_id: str,
                         node_id: str, idempotency_key: str, *,
                         scheduled_business_date: str | None = None) -> TransferResult:
        with self.database.atomic() as session:
            CircuitBreaker(self.database).require_write(session, user_id)
            existing = session.scalar(select(Operation).where(Operation.idempotency_key == idempotency_key))
            if existing is not None:
                return TransferResult(existing.id, existing.status, existing.amount_minor, [], existing.error_code)
            account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
            recipient = session.get(Payee, recipient_id)
            if account is None or recipient is None or recipient.owner_user_id != user_id:
                raise DomainError("NOT_OWNER")
            if recipient.is_new:
                raise DomainError("RECIPIENT_RISK_UPGRADE")
            current_date = scheduled_business_date or business_date()
            bucket = session.get(DailyRiskBucket, (user_id, current_date))
            if bucket is None:
                bucket = DailyRiskBucket(user_id=user_id, business_date=current_date,
                                         completed_minor=0, pending_minor=0)
                session.add(bucket)
            if account.status != "active" or account.available_minor < amount_minor:
                raise DomainError("INSUFFICIENT_FUNDS")
            projected = bucket.completed_minor + bucket.pending_minor + amount_minor
            if projected > 100_000:
                raise DomainError("DAILY_TOTAL_OVER_1000")
            operation_id = f"op_{uuid.uuid4().hex}"
            account.available_minor -= amount_minor
            bucket.completed_minor += amount_minor
            session.add(Operation(id=operation_id, user_id=user_id, idempotency_key=idempotency_key,
                                  plan_id=plan_id, node_id=node_id, status="succeeded", amount_minor=amount_minor,
                                  recipient_id=recipient_id))
            debit_id, credit_id = f"le_{uuid.uuid4().hex}", f"le_{uuid.uuid4().hex}"
            session.add_all([LedgerEntry(id=debit_id, operation_id=operation_id, account_id=account.id,
                                         amount_minor=-amount_minor, direction="debit"),
                             LedgerEntry(id=credit_id, operation_id=operation_id, account_id=recipient.account_id,
                                         amount_minor=amount_minor, direction="credit")])
            self.audit.append(session, f"trace_{plan_id}", user_id, "bank.scheduled.committed",
                              {"operation_id": operation_id, "ledger_entry_ids": [debit_id, credit_id]})
            return TransferResult(operation_id, "succeeded", amount_minor, [debit_id, credit_id])

    def _commit_transfer(self, session: Session, user_id: str, plan: PlanRecord, receipt: ReceiptRecord,
                         node: PlanNodeRecord, args: dict[str, object]) -> TransferResult:
        self._validate(session, user_id, plan, receipt, node)
        cooling = session.scalar(select(CoolingPeriod).where(CoolingPeriod.plan_id == plan.id,
                                                             CoolingPeriod.status == "active"))
        amount = int(str(args["amount_minor"]))
        if cooling is not None:
            if cooling.reconfirmed_at is None or receipt.assurance != "mfa":
                raise DomainError("COOLING_PERIOD_ACTIVE")
            self._release_hold(session, cooling, args)
            cooling.status = "completed"
        account = session.get(Account, str(args["source_account_id"]))
        if account is None or account.user_id != user_id:
            raise DomainError("NOT_OWNER")
        if account.status != "active" or account.available_minor < amount:
            raise DomainError("INSUFFICIENT_FUNDS")
        recipient = session.get(Payee, str(args["recipient_id"]))
        if recipient is None:
            raise DomainError("RECIPIENT_NOT_FOUND")
        bucket = self._bucket(session, user_id)
        projected = bucket.completed_minor + bucket.pending_minor + amount
        if projected > 100_000 and receipt.assurance != Assurance.mfa.value:
            raise DomainError("DAILY_TOTAL_OVER_1000")
        operation_id = f"op_{uuid.uuid4().hex}"
        operation = Operation(id=operation_id, user_id=user_id, idempotency_key=node.idempotency_key,
                              plan_id=plan.id, node_id=node.id, status="succeeded", amount_minor=amount,
                              recipient_id=str(args["recipient_id"]))
        account.available_minor -= amount
        account.ledger_balance_minor -= amount
        bucket.completed_minor += amount
        session.add(operation)
        debit_id = f"le_{uuid.uuid4().hex}"
        credit_id = f"le_{uuid.uuid4().hex}"
        session.add_all([LedgerEntry(id=debit_id, operation_id=operation_id, account_id=account.id,
                                     amount_minor=-amount, direction="debit"),
                         LedgerEntry(id=credit_id, operation_id=operation_id, account_id=recipient.account_id,
                                     amount_minor=amount, direction="credit")])
        node.status = "succeeded"
        plan.status = "succeeded"
        receipt.status = "consumed"
        session.add(ReceiptConsumption(receipt_id=receipt.id, node_id=node.id, operation_id=operation_id,
                                       consumed_at=utc_now_naive()))
        self.audit.append(session, f"trace_{plan.id}", user_id, "authorization.consumed",
                          {"receipt_id": receipt.id, "node_id": node.id})
        self.audit.append(session, f"trace_{plan.id}", user_id, "bank.command.committed",
                          {"operation_id": operation_id, "ledger_entry_ids": [debit_id, credit_id], "status": "succeeded"})
        return TransferResult(operation_id, "succeeded", amount, [debit_id, credit_id])

    @staticmethod
    def _bucket(session: Session, user_id: str) -> DailyRiskBucket:
        current_date = business_date()
        bucket = session.get(DailyRiskBucket, (user_id, current_date))
        if bucket is None:
            bucket = DailyRiskBucket(user_id=user_id, business_date=current_date)
            session.add(bucket)
            session.flush()
        return bucket

    @staticmethod
    def _create_challenge(session: Session, user_id: str, receipt_id: str, action_hash: str = "") -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        session.add(Challenge(id=f"ch_{uuid.uuid4().hex}", user_id=user_id, receipt_id=receipt_id,
                              action_hash=action_hash, code_hash=_hash(code), delivery_code=code,
                              expires_at=utc_now_naive() + timedelta(minutes=5)))
        return code
