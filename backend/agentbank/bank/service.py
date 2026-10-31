from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from agentbank.audit.store import PersistentAudit
from agentbank.auth.devices import DeviceService, _hash
from agentbank.clock import business_date, utc_now_naive
from agentbank.contracts import Assurance
from agentbank.security.fraud import FraudDetector, FraudLevel
from agentbank.security.cooling import CoolingPeriodService
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

    def confirm(self, user_id: str, plan_id: str, receipt_id: str, display_hash: str) -> TransferResult:
        with self.database.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            plan = session.get(PlanRecord, plan_id)
            receipt = session.get(ReceiptRecord, receipt_id)
            if plan is None or receipt is None or plan.user_id != user_id or receipt.subject_id != user_id:
                session.rollback()
                raise DomainError("NOT_OWNER")
            if receipt.status != "pending":
                session.rollback()
                raise DomainError("RECEIPT_REPLAYED")
            if receipt.display_hash != display_hash or plan.display_hash != display_hash:
                session.rollback()
                raise DomainError("HASH_MISMATCH")
            if receipt.expires_at <= utc_now_naive():
                session.rollback()
                raise DomainError("RECEIPT_EXPIRED")
            if receipt.assurance == Assurance.mfa.value:
                session.rollback()
                return TransferResult("", "mfa_required", 0, [], "MFA_REQUIRED")
            node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id))
            if node is None:
                session.rollback()
                raise DomainError("PRECONDITION_FAILED")
            args = json.loads(node.args_json)
            if action_hash(node.tool, args) != receipt.action_hash or action_hash(node.tool, args) != plan.action_hash:
                session.rollback()
                raise DomainError("HASH_MISMATCH")
            payee_for_display = session.get(Payee, str(args.get("recipient_id", "")))
            expected_display = _display_hash([{"label": "收款人", "value": payee_for_display.masked_name if payee_for_display else ""},
                                              {"label": "金额", "value": f"¥{int(args.get('amount_minor', 0)) / 100:.2f}"},
                                              {"label": "币种", "value": "人民币"}])
            if expected_display != receipt.display_hash or expected_display != plan.display_hash:
                session.rollback()
                raise DomainError("HASH_MISMATCH")
            assessment = self.fraud.evaluate(str(args.get("memo") or ""), relation={}, behavior={})
            if assessment.level is FraudLevel.high:
                self.audit.append(session, f"trace_{plan_id}", user_id, "security.fraud.blocked",
                                  {"level": assessment.level.value, "reason_codes": assessment.reason_codes})
                session.commit()
                raise DomainError("FRAUD_HIGH")
            if assessment.level is FraudLevel.medium:
                cooling = CoolingPeriodService(self.database).start_in_session(
                    session, user_id, plan_id, int(args["amount_minor"]),
                    action_hash=receipt.action_hash, plan_revision=receipt.plan_revision)
                self.audit.append(session, f"trace_{plan_id}", user_id, "security.cooling.started",
                                  {"cooling_id": cooling.id, "expires_at": cooling.expires_at.isoformat(),
                                   "reason_codes": assessment.reason_codes})
                session.commit()
                return TransferResult(cooling.id, "cooling_required", 0, [], "COOLING_PERIOD_REQUIRED")
            bucket = self._bucket(session, user_id)
            projected = bucket.completed_minor + bucket.pending_minor + int(args["amount_minor"])
            if projected > 100_000:
                receipt.assurance = Assurance.mfa.value
                plan.status = "awaiting_mfa"
                self._create_challenge(session, user_id, receipt.id, receipt.action_hash)
                self.audit.append(session, f"trace_{plan_id}", user_id, "policy.escalated",
                                  {"reason": "DAILY_TOTAL_OVER_1000", "projected_today_minor": projected})
                session.commit()
                return TransferResult("", "mfa_required", 0, [], "DAILY_TOTAL_OVER_1000")
            result = self._commit_transfer(session, user_id, plan, receipt, node, args)
            session.commit()
            return result

    def verify(self, user_id: str, plan_id: str, code: str) -> TransferResult:
        with self.database.sessions() as session:
            plan = session.get(PlanRecord, plan_id)
            receipt = session.scalar(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan_id,
                                                                 ReceiptRecord.status == "pending").order_by(ReceiptRecord.issued_at.desc()))
            if receipt is None or plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            receipt_id = receipt.id
            action_digest = receipt.action_hash
        challenge = self.devices.verify(user_id, code, receipt_id, action_digest)
        with self.database.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            receipt = session.get(ReceiptRecord, challenge.receipt_id)
            plan = session.get(PlanRecord, plan_id)
            node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id))
            if receipt is None or plan is None or node is None or receipt.plan_id != plan_id or plan.user_id != user_id:
                session.rollback()
                raise DomainError("NOT_OWNER")
            if receipt.status != "pending":
                session.rollback()
                raise DomainError("RECEIPT_REPLAYED")
            result = self._commit_transfer(session, user_id, plan, receipt, node, json.loads(node.args_json))
            session.commit()
            return result

    def release_cooling(self, user_id: str, cooling_id: str, *, action_hash_value: str = "",
                        plan_revision: int | None = None,
                        now: datetime | None = None, reconfirmed: bool = False) -> TransferResult:
        """Reconfirm and commit a transfer that was held by the cooling period."""
        current = now or datetime.now(timezone.utc)
        with self.database.atomic() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None or cooling.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if cooling.status != "active":
                raise DomainError("COOLING_NOT_ACTIVE")
            if action_hash_value and cooling.action_hash and action_hash_value != cooling.action_hash:
                raise DomainError("ACTION_CHANGED")
            if plan_revision is not None and plan_revision != cooling.plan_revision:
                raise DomainError("ACTION_CHANGED")
            if _utc_aware(cooling.expires_at) > current:
                raise DomainError("COOLING_PERIOD_ACTIVE")
            deadline = cooling.reconfirm_deadline or cooling.expires_at + timedelta(minutes=10)
            if current > _utc_aware(deadline) or not reconfirmed:
                raise DomainError("COOLING_RECONFIRMATION_REQUIRED")
            plan = session.get(PlanRecord, cooling.plan_id)
            receipt = session.scalar(select(ReceiptRecord).where(
                ReceiptRecord.plan_id == cooling.plan_id,
                ReceiptRecord.status == "pending",
            ).order_by(ReceiptRecord.issued_at.desc()))
            node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == cooling.plan_id))
            if plan is None or receipt is None or node is None or plan.user_id != user_id:
                raise DomainError("RECEIPT_INVALID")
            if cooling.action_hash and receipt.action_hash != cooling.action_hash:
                raise DomainError("ACTION_CHANGED")
            result = self._commit_transfer(session, user_id, plan, receipt, node, json.loads(node.args_json))
            cooling.status = "completed"
            cooling.reconfirmed_at = current.replace(tzinfo=None)
            return result

    def commit_scheduled(self, user_id: str, recipient_id: str, amount_minor: int, plan_id: str,
                         node_id: str, idempotency_key: str, *,
                         scheduled_business_date: str | None = None) -> TransferResult:
        with self.database.atomic() as session:
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
        amount = int(str(args["amount_minor"]))
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
