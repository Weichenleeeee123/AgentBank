from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from agentbank.audit.store import PersistentAudit
from agentbank.auth.devices import DeviceService, _hash
from agentbank.clock import business_date, utc_now_naive
from agentbank.contracts import Assurance
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
)
from agentbank.errors import AmbiguousRecipient, DomainError
from agentbank.policy.engine import PolicyContext, action_hash, decide


def _display_hash(display: list[dict[str, str]]) -> str:
    canonical = json.dumps(display, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
            payee = payees[0]
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
                self._create_challenge(session, user_id, receipt_id)
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
            if receipt.status == "consumed":
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
            bucket = self._bucket(session, user_id)
            projected = bucket.completed_minor + bucket.pending_minor + int(args["amount_minor"])
            if projected > 100_000:
                receipt.assurance = Assurance.mfa.value
                plan.status = "awaiting_mfa"
                self._create_challenge(session, user_id, receipt.id)
                self.audit.append(session, f"trace_{plan_id}", user_id, "policy.escalated",
                                  {"reason": "DAILY_TOTAL_OVER_1000", "projected_today_minor": projected})
                session.commit()
                return TransferResult("", "mfa_required", 0, [], "DAILY_TOTAL_OVER_1000")
            result = self._commit_transfer(session, user_id, plan, receipt, node, args)
            session.commit()
            return result

    def verify(self, user_id: str, plan_id: str, code: str) -> TransferResult:
        challenge = self.devices.verify(user_id, code)
        with self.database.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            receipt = session.scalar(select(ReceiptRecord).where(ReceiptRecord.id == challenge.receipt_id))
            plan = session.get(PlanRecord, plan_id)
            node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id))
            if receipt is None or plan is None or node is None or receipt.plan_id != plan_id or plan.user_id != user_id:
                session.rollback()
                raise DomainError("NOT_OWNER")
            if receipt.status == "consumed":
                session.rollback()
                raise DomainError("RECEIPT_REPLAYED")
            result = self._commit_transfer(session, user_id, plan, receipt, node, json.loads(node.args_json))
            session.commit()
            return result

    def _commit_transfer(self, session: Session, user_id: str, plan: PlanRecord, receipt: ReceiptRecord,
                         node: PlanNodeRecord, args: dict[str, object]) -> TransferResult:
        amount = int(str(args["amount_minor"]))
        account = session.get(Account, str(args["source_account_id"]))
        if account is None or account.user_id != user_id:
            raise DomainError("NOT_OWNER")
        if account.status != "active" or account.available_minor < amount:
            raise DomainError("INSUFFICIENT_FUNDS")
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
                         LedgerEntry(id=credit_id, operation_id=operation_id, account_id=str(args["recipient_id"]),
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
    def _create_challenge(session: Session, user_id: str, receipt_id: str) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        session.add(Challenge(id=f"ch_{uuid.uuid4().hex}", user_id=user_id, receipt_id=receipt_id,
                              code_hash=_hash(code), delivery_code=code,
                              expires_at=utc_now_naive() + timedelta(minutes=5)))
        return code
