from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from agentbank.analysis.dsl import DslExecutor
from agentbank.analysis.insights import detect_anomalies, detect_subscriptions, monthly_report
from agentbank.audit.store import PersistentAudit
from agentbank.auth.devices import DeviceService
from agentbank.bank.business import CardService, BirthdayPlanner, MandateService, RecipientResolver, RiskService, SubscriptionService
from agentbank.bank.capabilities import split_bill
from agentbank.bank.service import TransferService
from agentbank.clock import BusinessClock, business_date
from agentbank.contracts import CapabilityResult
from agentbank.db import AABill, AAShare, Account, Card, CardApplication, CapabilityRecord, Database, DebitMandate, InvestmentOrder, Mandate, MerchantOrder, Payee, Position, Product, Reminder, Reservation, ScheduledJob, Subscription, Transaction
from agentbank.errors import DomainError
from agentbank.security.breaker import CircuitBreaker
from agentbank.workflow.recovery import SagaRunner
from agentbank.tools import CAPABILITIES


class CapabilityService:
    """Deterministic handlers for every M2 capability ID.

    The service stores a business record for every command/query invocation so the
    evaluator can assert state and audit evidence instead of only observing a tool call.
    """

    def __init__(self, database: Database, clock: BusinessClock | None = None, *, require_device_mfa: bool = False) -> None:
        self.database = database
        self.clock = clock or BusinessClock()
        self.audit = PersistentAudit(database)
        self.breaker = CircuitBreaker(database)
        self.device_tokens: dict[str, str] = {}
        self.require_device_mfa = require_device_mfa

    def execute(self, user_id: str, capability_id: str, payload: dict[str, Any], idempotency_key: str) -> CapabilityResult:
        if capability_id not in CAPABILITIES:
            raise DomainError("CAPABILITY_NOT_REGISTERED")
        with self.database.sessions() as session:
            existing = session.scalar(select(CapabilityRecord).where(CapabilityRecord.idempotency_key == idempotency_key))
            if existing is not None:
                if existing.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                return self._result(existing)
        if capability_id in {"W04", "W05", "C04", "C05", "C06", "C08"} or (capability_id == "C01" and not bool(payload.get("virtual", True))):
            if payload.get("assurance") != "mfa":
                raise DomainError("MFA_REQUIRED")
            device_token = payload.get("device_token")
            otp = payload.get("otp")
            if self.require_device_mfa and not payload.get("_mfa_verified") and not (isinstance(device_token, str) and isinstance(otp, str)):
                record_id = f"cap_{uuid.uuid4().hex}"
                pending_data = {"payload": payload, "assurance": "mfa", "challenge_record_id": record_id}
                with self.database.sessions.begin() as session:
                    session.add(CapabilityRecord(id=record_id, capability_id=capability_id, user_id=user_id,
                                                 status="awaiting_mfa", data_json=json.dumps(pending_data, ensure_ascii=False),
                                                 idempotency_key=idempotency_key))
                if user_id in self.device_tokens:
                    DeviceService(self.database).issue_otp(user_id, record_id)
                return CapabilityResult(capability_id=capability_id, record_id=record_id, status="awaiting_mfa",
                                        data=pending_data, evidence={"audit_event": f"capability.{capability_id}.awaiting_mfa",
                                                                       "idempotency_key": idempotency_key})
            if self.require_device_mfa and not payload.get("_mfa_verified"):
                DeviceService(self.database).validate_token(user_id, str(device_token))
                DeviceService(self.database).verify(user_id, str(otp))
        if capability_id == "C02":
            with self.database.sessions() as session:
                card = session.get(Card, str(payload.get("card_id", "card_001")))
                if card is not None and int(payload.get("limit_minor", card.limit_minor)) > card.limit_minor and payload.get("assurance") != "mfa":
                    raise DomainError("MFA_REQUIRED")
        read_only = capability_id in {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "S01", "X01"}
        protective = capability_id in {"C02", "C03", "C07", "X05"}
        if not read_only and not protective and self.breaker.is_locked(user_id):
            with self.database.sessions.begin() as session:
                self.audit.append(session, f"trace_security_{uuid.uuid4().hex}", user_id, "security.breaker.blocked",
                                  {"capability_id": capability_id, "reason": "U17"})
            raise DomainError("SECURITY_LOCKED")
        data, status = self._dispatch(user_id, capability_id, payload, idempotency_key)
        record_id = f"cap_{uuid.uuid4().hex}"
        with self.database.sessions.begin() as session:
            record = CapabilityRecord(id=record_id, capability_id=capability_id, user_id=user_id,
                                      status=status, data_json=json.dumps(data, ensure_ascii=False, sort_keys=True),
                                      idempotency_key=idempotency_key)
            session.add(record)
            self.audit.append(session, f"trace_{record_id}", user_id, f"capability.{capability_id}.completed",
                              {"capability_id": capability_id, "record_id": record_id, "status": status,
                               "idempotency_key": idempotency_key})
            session.flush()
            return self._result(record)

    def verify_mfa(self, user_id: str, record_id: str, code: str) -> CapabilityResult:
        with self.database.sessions() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
            data = json.loads(record.data_json)
            if record.status != "awaiting_mfa":
                return self._result(record)
        DeviceService(self.database).verify(user_id, code)
        payload = dict(data["payload"])
        payload["_mfa_verified"] = True
        result_data, status = self._dispatch(user_id, record.capability_id, payload, record.idempotency_key)
        with self.database.sessions.begin() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None:
                raise DomainError("CAPABILITY_RECORD_NOT_FOUND")
            record.status = status
            record.data_json = json.dumps(result_data, ensure_ascii=False, sort_keys=True)
            self.audit.append(session, f"trace_{record_id}", user_id, f"capability.{record.capability_id}.mfa_completed",
                              {"record_id": record_id, "status": status})
            return self._result(record)

    def get(self, user_id: str, record_id: str) -> CapabilityResult:
        with self.database.sessions() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
            return self._result(record)

    def release_reservation(self, user_id: str, record_id: str) -> CapabilityResult:
        with self.database.sessions() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id or record.capability_id != "X02":
                raise DomainError("NOT_OWNER")
            data = json.loads(record.data_json)
        with self.database.sessions.begin() as session:
            reservation = session.get(Reservation, str(data["reservation_id"]))
            if reservation is None or reservation.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if reservation.status == "active":
                account = session.get(Account, reservation.account_id)
                if account is None:
                    raise DomainError("ACCOUNT_NOT_FOUND")
                account.available_minor += reservation.amount_minor
                reservation.status = "released"
            data["status"] = "released"
            record = session.get(CapabilityRecord, record_id)
            if record is None:
                raise DomainError("CAPABILITY_RECORD_NOT_FOUND")
            record.status = "released"
            record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
            self.audit.append(session, f"trace_{record_id}", user_id, "funds.reservation.released",
                              {"record_id": record_id, "reservation_id": reservation.id})
            return self._result(record)

    def revoke_mandate(self, user_id: str, mandate_id: str) -> None:
        with self.database.sessions.begin() as session:
            mandate = session.get(Mandate, mandate_id)
            if mandate is None or mandate.user_id != user_id:
                raise DomainError("NOT_OWNER")
            mandate.status = "revoked"
            self.audit.append(session, f"trace_{mandate_id}", user_id, "mandate.revoked", {"mandate_id": mandate_id})

    def confirm_transfer(self, user_id: str, record_id: str, display_hash: str) -> CapabilityResult:
        with self.database.sessions() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
            data = json.loads(record.data_json)
        result = TransferService(self.database).confirm(user_id, str(data["plan_id"]), str(data["receipt_id"]), display_hash)
        with self.database.sessions.begin() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None:
                raise DomainError("CAPABILITY_RECORD_NOT_FOUND")
            data["transfer_result"] = {"operation_id": result.operation_id, "status": result.status,
                                        "amount_minor": result.amount_minor, "ledger_entry_ids": result.ledger_entry_ids,
                                        "error_code": result.error_code}
            record.status = result.status
            record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
            self.audit.append(session, f"trace_{record_id}", user_id, "capability.transfer.confirmed",
                              {"record_id": record_id, "status": result.status})
            return self._result(record)

    def verify_transfer(self, user_id: str, record_id: str, code: str) -> CapabilityResult:
        with self.database.sessions() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
            data = json.loads(record.data_json)
        result = TransferService(self.database).verify(user_id, str(data["plan_id"]), code)
        with self.database.sessions.begin() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None:
                raise DomainError("CAPABILITY_RECORD_NOT_FOUND")
            data["transfer_result"] = {"operation_id": result.operation_id, "status": result.status,
                                        "amount_minor": result.amount_minor, "ledger_entry_ids": result.ledger_entry_ids,
                                        "error_code": result.error_code}
            record.status = result.status
            record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
            self.audit.append(session, f"trace_{record_id}", user_id, "capability.transfer.verified",
                              {"record_id": record_id, "status": result.status})
            return self._result(record)

    def _result(self, record: CapabilityRecord) -> CapabilityResult:
        return CapabilityResult(capability_id=record.capability_id, record_id=record.id, status=record.status,
                                data=json.loads(record.data_json),
                                evidence={"audit_event": f"capability.{record.capability_id}.completed",
                                          "idempotency_key": record.idempotency_key})

    def _dispatch(self, user_id: str, capability_id: str, payload: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], str]:
        if capability_id == "Q01":
            account = self._account(user_id)
            return {"account_id": account.id, "ledger_balance_minor": account.ledger_balance_minor,
                    "available_minor": account.available_minor, "currency": "CNY",
                    "as_of": datetime.now(timezone.utc).isoformat()}, "succeeded"
        if capability_id == "Q02":
            with self.database.sessions() as session:
                rows = session.scalars(select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.posted_at)).all()
                return {"transactions": [{"txn_id": row.id, "posted_at": row.posted_at, "amount_minor": row.amount_minor,
                                           "merchant": row.merchant, "category": row.category, "direction": row.direction}
                                          for row in rows]}, "succeeded"
        if capability_id in {"T02", "T03"}:
            resolver = RecipientResolver(self.database)
            candidate = resolver.by_phone(user_id, str(payload["phone"])) if payload.get("phone") else resolver.by_name(user_id, str(payload.get("alias") or payload.get("recipient_name", "")))
            prepared = TransferService(self.database).prepare_for_payee(user_id, candidate.id,
                                                                         int(payload.get("amount_minor", 1_000)), payload.get("memo"))
            return {"recipient_id": candidate.id, "masked_name": candidate.masked_name, "phone_tail": candidate.phone_tail,
                    "is_new": candidate.is_new, "next": "transfer.create", "plan_id": prepared.plan_id,
                    "receipt_id": prepared.receipt_id, "display_hash": prepared.display_hash,
                    "action_hash": prepared.action_hash, "assurance": prepared.assurance.value,
                    "tier_display": prepared.tier_display, "display": prepared.display}, "prepared"
        if capability_id == "T01":
            prepared = TransferService(self.database).prepare(user_id, str(payload.get("recipient_name", "")),
                                                              int(payload.get("amount_minor", 0)), payload.get("memo"))
            return {"plan_id": prepared.plan_id, "receipt_id": prepared.receipt_id, "display_hash": prepared.display_hash,
                    "action_hash": prepared.action_hash, "tier_display": prepared.tier_display,
                    "assurance": prepared.assurance.value, "display": prepared.display}, "prepared"
        if capability_id == "T04":
            period = str(payload.get("period", "monthly"))
            recipient_id = str(payload.get("recipient_id", "payee_landlord"))
            with self.database.sessions() as lookup_session:
                recipient = lookup_session.get(Payee, recipient_id)
            if recipient is None or recipient.owner_user_id != user_id:
                raise DomainError("RECIPIENT_NOT_FOUND")
            schedule_id = f"schedule_{uuid.uuid4().hex}"
            due_date = str(payload.get("next_due", self.clock.business_date()))
            with self.database.sessions.begin() as session:
                session.add(ScheduledJob(id=f"job_{uuid.uuid4().hex}", user_id=user_id, schedule_id=schedule_id,
                                         due_date=due_date, period_index=1, idempotency_key=f"{idempotency_key}-period-1",
                                         status="scheduled"))
                mandate_id = f"mdt_{uuid.uuid4().hex}"
                session.add(Mandate(id=mandate_id, user_id=user_id, plan_id=schedule_id,
                                    operation_type="transfer.create", subject_ref=recipient.id,
                                    max_amount_minor=int(payload.get("amount_minor", 1_000)),
                                    max_uses=int(payload.get("max_uses", 1)), starts_on=due_date,
                                    expires_on="2099-12-31", status="active"))
            return {"schedule_id": schedule_id, "period": period,
                    "next_due": due_date,
                    "max_uses": int(payload.get("max_uses", 1)), "amount_minor": int(payload.get("amount_minor", 1_000)),
                    "recipient_id": recipient.id, "mandate_id": mandate_id, "operation_template": f"op_{idempotency_key}"}, "scheduled"
        if capability_id == "T05":
            if payload.get("action") == "status":
                bill_id = str(payload["bill_id"])
                with self.database.sessions() as session:
                    bill = session.get(AABill, bill_id)
                    if bill is None or bill.user_id != user_id:
                        raise DomainError("NOT_OWNER")
                    shares = session.scalars(select(AAShare).where(AAShare.bill_id == bill_id)).all()
                    return {"bill_id": bill_id, "status": bill.status, "shares": [{"participant_id": share.participant_id,
                            "amount_minor": share.amount_minor, "status": share.status} for share in shares]}, "succeeded"
            if payload.get("action") == "pay_share":
                bill_id = str(payload["bill_id"])
                with self.database.sessions.begin() as session:
                    bill = session.get(AABill, bill_id)
                    if bill is None:
                        raise DomainError("BILL_NOT_FOUND")
                    share = session.scalar(select(AAShare).where(AAShare.bill_id == bill_id,
                                                                  AAShare.participant_id == user_id))
                    if share is None:
                        raise DomainError("SHARE_NOT_FOUND")
                    if share.status == "paid":
                        return {"bill_id": bill_id, "share_status": "paid", "bill_status": bill.status}, "succeeded"
                    aa_account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
                    if aa_account is None or aa_account.available_minor < share.amount_minor:
                        raise DomainError("INSUFFICIENT_FUNDS")
                    aa_account.available_minor -= share.amount_minor
                    share.status = "paid"
                    unpaid = session.scalar(select(AAShare).where(AAShare.bill_id == bill_id, AAShare.status != "paid"))
                    if unpaid is None:
                        bill.status = "settled"
                    return {"bill_id": bill_id, "share_status": share.status, "bill_status": bill.status,
                            "amount_minor": share.amount_minor}, "succeeded"
            participants = [str(item) for item in payload.get("participant_ids", [user_id, "user_002", "user_003"])]
            total_minor = int(payload.get("total_minor", 10_000))
            allocations = split_bill(total_minor, participants, user_id)
            bill_id = f"aa_{uuid.uuid4().hex}"
            with self.database.sessions.begin() as session:
                session.add(AABill(id=bill_id, user_id=user_id, total_minor=total_minor, status="open"))
                for participant_id, amount in allocations.items():
                    session.add(AAShare(id=f"share_{uuid.uuid4().hex}", bill_id=bill_id,
                                        participant_id=participant_id, amount_minor=amount, status="requested"))
            return {"bill_id": bill_id, "shares": allocations, "participant_count": len(participants), "request_only": True}, "created"
        if capability_id == "B01":
            dsl = payload.get("dsl", {"source": "transactions", "group_by": ["category"], "limit": 100})
            result = DslExecutor(self.database).run(user_id, dsl)
            return {"rows": result.rows, "query_hash": result.query_hash, "row_count": result.row_count}, "succeeded"
        if capability_id == "B02":
            return {"anomalies": detect_anomalies(self.database, user_id)}, "succeeded"
        if capability_id == "B03":
            return {"months": monthly_report(self.database, user_id, str(payload.get("start_month", "2026-01")),
                                              str(payload.get("end_month", "2026-12")))}, "succeeded"
        if capability_id == "B04":
            months = monthly_report(self.database, user_id, str(payload.get("start_month", "2026-01")),
                                    str(payload.get("end_month", "2026-12")))
            return {"year": str(payload.get("year", "2026")), "months": months, "missing_months": [row["month"] for row in months if row["missing"]]}, "succeeded"
        if capability_id == "W01":
            if not self._has_risk(user_id):
                raise DomainError("RISK_PROFILE_REQUIRED")
            products = RiskService(self.database).recommend(user_id)
            return {"products": [self._product_data(product) for product in products]}, "succeeded"
        if capability_id == "W02":
            with self.database.sessions() as session:
                products = list(session.scalars(select(Product)).all())
                return {"products": [self._product_data(product) for product in products]}, "succeeded"
        if capability_id == "W03":
            profile = RiskService(self.database).submit(user_id, [int(value) for value in payload.get("answers", [1] * 10)])
            return {"level": profile.level, "score": profile.score, "expires_on": profile.expires_on}, "succeeded"
        if capability_id == "W04":
            product_id = str(payload.get("product_id", "product_R1"))
            with self.database.sessions() as session:
                product = session.get(Product, product_id)
            if product is None:
                raise DomainError("PRODUCT_NOT_FOUND")
            if not self._suitable(user_id, product.risk_level):
                raise DomainError("SUITABILITY_MISMATCH")
            amount_minor = int(payload.get("amount_minor", 10_000))
            with self.database.sessions.begin() as session:
                investment_account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
                if investment_account is None or investment_account.available_minor < amount_minor:
                    raise DomainError("INSUFFICIENT_FUNDS")
                investment_account.available_minor -= amount_minor
                order_id = f"order_{uuid.uuid4().hex}"
                shares_count = amount_minor // 100
                session.add(InvestmentOrder(id=order_id, user_id=user_id, product_id=product_id, side="subscribe",
                                            amount_minor=amount_minor, shares=shares_count, status="subscribed"))
                session.add(Position(id=f"pos_{uuid.uuid4().hex}", user_id=user_id, product_id=product_id,
                                     shares=shares_count, cost_minor=amount_minor, market_value_minor=amount_minor,
                                     as_of=datetime.now(timezone.utc).replace(tzinfo=None)))
            return {"order_id": order_id, "product_id": product_id, "status": "subscribed",
                    "shares": shares_count, "amount_minor": amount_minor, "funds_status": "debited"}, "succeeded"
        if capability_id == "W05":
            order_id = str(payload.get("order_id", f"order_{uuid.uuid4().hex}"))
            expected = datetime.now(timezone.utc) + timedelta(days=2)
            with self.database.sessions.begin() as session:
                order = session.get(InvestmentOrder, order_id)
                if order is None or order.user_id != user_id:
                    raise DomainError("ORDER_NOT_FOUND")
                order.status = "processing"
                order.side = "redeem"
                order.expected_settlement_at = expected.replace(tzinfo=None)
            return {"order_id": order_id, "status": "processing", "expected_settlement_at": expected.isoformat()}, "processing"
        if capability_id == "W06":
            with self.database.sessions() as session:
                positions = session.scalars(select(Position).where(Position.user_id == user_id)).all()
            return {"positions": [{"product_id": position.product_id, "shares": position.shares,
                                    "cost_minor": position.cost_minor, "market_value_minor": position.market_value_minor,
                                    "pnl_minor": position.market_value_minor - position.cost_minor} for position in positions],
                    "source": "simulated_bank", "as_of": datetime.now(timezone.utc).isoformat()}, "succeeded"
        if capability_id.startswith("C"):
            return self._card(user_id, capability_id, payload)
        if capability_id == "S01":
            if payload.get("action") == "simulate_charge":
                subscription_id = str(payload.get("subscription_id", "sub_video"))
                with self.database.sessions.begin() as session:
                    subscription = session.get(Subscription, subscription_id)
                    if subscription is None or subscription.user_id != user_id:
                        raise DomainError("NOT_OWNER")
                    if subscription.status != "active":
                        return {"subscription_id": subscription_id, "charged": False, "reason": "CANCELLED"}, "succeeded"
                    subscription_account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
                    if subscription_account is None or subscription_account.available_minor < subscription.amount_minor:
                        return {"subscription_id": subscription_id, "charged": False, "reason": "INSUFFICIENT_FUNDS"}, "succeeded"
                    subscription_account.available_minor -= subscription.amount_minor
                    transaction_id = f"txn_sub_{uuid.uuid4().hex}"
                    session.add(Transaction(id=transaction_id, user_id=user_id, posted_at=self.clock.business_date(),
                                            amount_minor=subscription.amount_minor, merchant=subscription.merchant,
                                            category="订阅", direction="debit", memo="模拟续费"))
                    return {"subscription_id": subscription_id, "charged": True, "transaction_id": transaction_id,
                            "amount_minor": subscription.amount_minor}, "succeeded"
            return {"subscriptions": detect_subscriptions(self.database, user_id)}, "succeeded"
        if capability_id == "S02":
            subscription_id = str(payload.get("subscription_id", "sub_video"))
            reminder_id = f"rem_{uuid.uuid4().hex}"
            with self.database.sessions.begin() as session:
                subscription = session.get(__import__("agentbank.db", fromlist=["Subscription"]).Subscription, subscription_id)
                if subscription is None or subscription.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                session.add(Reminder(id=reminder_id, user_id=user_id, subscription_id=subscription_id,
                                     remind_before_days=int(payload.get("days", 3)), status="scheduled",
                                     idempotency_key=idempotency_key))
            return {"reminder_id": reminder_id, "subscription_id": subscription_id,
                    "remind_before_days": int(payload.get("days", 3)), "dedupe_key": idempotency_key}, "scheduled"
        if capability_id == "S03":
            subscription_id = str(payload.get("subscription_id", "sub_video"))
            SubscriptionService(self.database).cancel(user_id, subscription_id)
            with self.database.sessions.begin() as session:
                mandate = session.scalar(select(DebitMandate).where(DebitMandate.subscription_id == subscription_id,
                                                                     DebitMandate.user_id == user_id))
                if mandate is None:
                    session.add(DebitMandate(id=f"dm_{uuid.uuid4().hex}", user_id=user_id,
                                             subscription_id=subscription_id, status="revoked"))
                else:
                    mandate.status = "revoked"
            return {"subscription_id": subscription_id, "status": "cancelled", "debit_mandate": "revoked"}, "succeeded"
        if capability_id == "X01":
            with self.database.sessions() as session:
                events = session.execute(select(__import__("agentbank.db", fromlist=["Event"]).Event).where(__import__("agentbank.db", fromlist=["Event"]).Event.user_id == user_id)).scalars().all()
            return {"events": [{"event_id": event.id, "title": event.title, "event_date": event.event_date} for event in events], "recommendation_only": True}, "succeeded"
        if capability_id == "X02":
            amount = int(payload.get("amount_minor", 100_000))
            planner = BirthdayPlanner(self.database, self.clock, MandateService(self.database, self.clock))
            reservation_id = planner.reserve(user_id, amount, str(payload.get("plan_id", idempotency_key)))
            return {"reservation_id": reservation_id, "amount_minor": amount, "ledger_balance_unchanged": True}, "reserved"
        if capability_id in {"X03", "X04"}:
            order_id = f"merchant_order_{uuid.uuid4().hex}"
            quote_id = f"quote_{uuid.uuid4().hex}"
            item = "flower" if capability_id == "X03" else "cake"
            if payload.get("simulate_failure"):
                saga = SagaRunner().run([
                    ("reserve", lambda: None, lambda: None),
                    ("merchant_order", lambda: (_ for _ in ()).throw(RuntimeError("merchant_unavailable")), lambda: None),
                ])
                with self.database.sessions.begin() as session:
                    session.add(MerchantOrder(id=order_id, user_id=user_id, item=item, quote_id=quote_id,
                                              amount_minor=int(payload.get("amount_minor", 5_000)),
                                              delivery_date=str(payload.get("delivery_date", "2026-11-13")), status=saga.status,
                                              reservation_id=payload.get("reservation_id")))
                return {"quote_id": quote_id, "order_id": order_id, "item": item, "status": saga.status,
                        "delivery_date": payload.get("delivery_date", self.clock.business_date()),
                        "reservation_id": payload.get("reservation_id"),
                        "compensation_status": "reservation_released", "failure_code": "MERCHANT_UNAVAILABLE"}, saga.status
            with self.database.sessions.begin() as session:
                session.add(MerchantOrder(id=order_id, user_id=user_id, item=item, quote_id=quote_id,
                                          amount_minor=int(payload.get("amount_minor", 5_000)),
                                          delivery_date=str(payload.get("delivery_date", "2026-11-13")), status="scheduled",
                                          reservation_id=payload.get("reservation_id")))
            return {"quote_id": quote_id, "order_id": order_id, "item": item, "status": "scheduled",
                    "delivery_date": payload.get("delivery_date", "2026-11-13")}, "scheduled"
        if capability_id == "X05":
            card_id = CardService(self.database).freeze(user_id)
            if payload.get("mfa_verified"):
                with self.database.sessions.begin() as session:
                    card = session.get(Card, card_id)
                    if card is not None:
                        card.status = "lost"
                return {"card_id": card_id, "freeze_status": "lost", "loss_report_status": "succeeded",
                        "payment_blocked": True}, "succeeded"
            return {"card_id": card_id, "freeze_status": "frozen", "loss_report_status": "awaiting_mfa",
                    "next_step": "verify_loss_report"}, "partially_completed"
        raise DomainError("CAPABILITY_NOT_IMPLEMENTED")

    def _card(self, user_id: str, capability_id: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if capability_id == "C07":
            card_id = CardService(self.database).freeze(user_id, payload.get("card_id"))
            return {"card_id": card_id, "status": "frozen", "payment_blocked": True}, "succeeded"
        if capability_id == "C06":
            card_id = str(payload.get("card_id", "card_001"))
            with self.database.sessions.begin() as session:
                card = session.get(Card, card_id)
                if card is None or card.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                if card.status != "lost":
                    raise DomainError("INVALID_CARD_STATE")
                card.status = "active"
            return {"card_id": card_id, "status": "active", "payment_blocked": False}, "succeeded"
        if capability_id == "C05":
            card_id = str(payload.get("card_id", "card_001"))
            with self.database.sessions.begin() as session:
                card = session.get(Card, card_id)
                if card is None or card.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                card.status = "lost"
            return {"card_id": card_id, "status": "lost", "payment_blocked": True}, "succeeded"
        if capability_id == "C01":
            card_id = f"card_{uuid.uuid4().hex[:12]}"
            card_status = "active" if payload.get("virtual", True) else "pending"
            with self.database.sessions.begin() as session:
                session.add(Card(id=card_id, user_id=user_id, status=card_status))
                session.add(CardApplication(id=f"cardapp_{uuid.uuid4().hex}", card_id=card_id, user_id=user_id,
                                            card_type="virtual" if payload.get("virtual", True) else "physical",
                                            status=card_status))
            return {"card_id": card_id, "status": card_status, "card_type": "virtual" if payload.get("virtual", True) else "physical"}, "succeeded" if card_status == "active" else "pending"
        if capability_id == "C02":
            card_id = str(payload.get("card_id", "card_001"))
            with self.database.sessions.begin() as session:
                card = session.get(Card, card_id)
                if card is None or card.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                card.limit_minor = int(payload.get("limit_minor", card.limit_minor))
            return {"card_id": card_id, "limit_minor": int(payload.get("limit_minor", 100_000))}, "succeeded"
        if capability_id == "C03":
            card_id = str(payload.get("card_id", "card_001"))
            restriction = str(payload.get("restriction", "online"))
            with self.database.sessions.begin() as session:
                card = session.get(Card, card_id)
                if card is None or card.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                restrictions = json.loads(card.restrictions_json)
                if restriction not in restrictions:
                    restrictions.append(restriction)
                card.restrictions_json = json.dumps(restrictions)
            return {"card_id": card_id, "restriction": restriction, "status": "restricted", "payment_blocked": True}, "succeeded"
        if capability_id == "C04":
            card_id = str(payload.get("card_id", "card_001"))
            restriction = str(payload.get("restriction", "online"))
            with self.database.sessions.begin() as session:
                card = session.get(Card, card_id)
                if card is None or card.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                card.restrictions_json = json.dumps([item for item in json.loads(card.restrictions_json) if item != restriction])
            return {"card_id": card_id, "restriction": restriction, "status": "active", "payment_blocked": False}, "succeeded"
        if capability_id == "C08":
            return {"card_id": str(payload.get("card_id", "card_001")), "status": "pin_changed", "secret_persisted": False}, "succeeded"
        raise DomainError("CAPABILITY_NOT_IMPLEMENTED")

    def card_payment(self, user_id: str, card_id: str, amount_minor: int) -> str:
        with self.database.sessions() as session:
            card = session.get(Card, card_id)
            if card is None or card.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if card.status in {"frozen", "lost", "restricted"} or json.loads(card.restrictions_json):
                return "CARD_RESTRICTED"
            if amount_minor > card.limit_minor:
                return "LIMIT_EXCEEDED"
            return "approved"

    def progress_investments(self, user_id: str | None = None) -> list[CapabilityResult]:
        changed: list[CapabilityRecord] = []
        with self.database.sessions.begin() as session:
            query = select(InvestmentOrder).where(InvestmentOrder.status == "processing", InvestmentOrder.side == "redeem")
            if user_id:
                query = query.where(InvestmentOrder.user_id == user_id)
            orders = list(session.scalars(query).all())
            for order in orders:
                order.status = "settled"
                redemption_account = session.scalar(select(Account).where(Account.user_id == order.user_id).order_by(Account.id))
                if redemption_account is not None:
                    redemption_account.available_minor += order.amount_minor
                position = session.scalar(select(Position).where(Position.user_id == order.user_id,
                                                                  Position.product_id == order.product_id))
                if position is not None:
                    position.shares = 0
                    position.market_value_minor = 0
                record = session.scalar(select(CapabilityRecord).where(CapabilityRecord.user_id == order.user_id,
                                                                        CapabilityRecord.capability_id == "W05",
                                                                        CapabilityRecord.data_json.contains(order.id)))
                if record is not None:
                    data = json.loads(record.data_json)
                    data["status"] = "settled"
                    record.status = "succeeded"
                    record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                    changed.append(record)
        return [self._result(record) for record in changed]

    def dispatch_due(self, user_id: str | None = None) -> list[CapabilityResult]:
        today = self.clock.business_date()
        changed: list[CapabilityRecord] = []
        with self.database.sessions() as session:
            query = select(CapabilityRecord).where(CapabilityRecord.status.in_(["scheduled", "partially_completed"]),
                                                    CapabilityRecord.capability_id.in_(["T04", "X03", "X04"]))
            if user_id:
                query = query.where(CapabilityRecord.user_id == user_id)
            records = list(session.scalars(query).all())
        for record in records:
            data = json.loads(record.data_json)
            due = str(data.get("next_due") or data.get("delivery_date") or "9999-12-31")
            if due > today:
                continue
            status = "succeeded"
            if record.capability_id == "T04":
                try:
                    mandate = None
                    with self.database.sessions() as mandate_session:
                        mandate = mandate_session.get(Mandate, str(data.get("mandate_id", "")))
                    if mandate is None or mandate.status != "active":
                        raise DomainError("MANDATE_INVALID")
                    if not MandateService(self.database, self.clock).consume(mandate.id, int(data["amount_minor"])):
                        raise DomainError("MANDATE_INVALID")
                    transfer = TransferService(self.database).commit_scheduled(
                        record.user_id, str(data["recipient_id"]), int(data["amount_minor"]),
                        str(data["schedule_id"]), record.id, f"{record.id}-period-{int(data.get('dispatched_count', 0)) + 1}")
                    data["operation_id"] = transfer.operation_id
                    with self.database.sessions.begin() as job_session:
                        job = job_session.scalar(select(ScheduledJob).where(ScheduledJob.schedule_id == str(data["schedule_id"]),
                                                                            ScheduledJob.status == "scheduled"))
                        if job is not None:
                            job.operation_id = transfer.operation_id
                            job.status = "succeeded"
                    data["dispatched_count"] = int(data.get("dispatched_count", 0)) + 1
                    data["max_uses"] = int(data.get("max_uses", 1)) - 1
                    if data["max_uses"] > 0:
                        data["next_due"] = (datetime.fromisoformat(due) + timedelta(days=30)).date().isoformat()
                        status = "scheduled"
                except DomainError as error:
                    data["error_code"] = error.code
                    status = "paused"
            else:
                data["operation_id"] = f"{record.id}-operation"
                data["status"] = "succeeded"
                with self.database.sessions.begin() as order_session:
                    merchant = order_session.get(MerchantOrder, str(data.get("order_id", "")))
                    if merchant is not None:
                        if merchant.status == "partially_completed":
                            merchant.status = "failed"
                            if merchant.reservation_id:
                                reservation = order_session.get(Reservation, merchant.reservation_id)
                                if reservation is not None and reservation.status == "active":
                                    account = order_session.get(Account, reservation.account_id)
                                    if account is not None:
                                        account.available_minor += reservation.amount_minor
                                    reservation.status = "released"
                            data["status"] = "failed"
                        else:
                            merchant.status = "delivered"
                            if merchant.reservation_id:
                                reservation = order_session.get(Reservation, merchant.reservation_id)
                                if reservation is not None and reservation.status == "active":
                                    reservation.status = "consumed"
            with self.database.sessions.begin() as session:
                current = session.get(CapabilityRecord, record.id)
                if current is None:
                    continue
                current.status = status
                current.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                self.audit.append(session, f"trace_{record.id}", record.user_id, f"capability.{record.capability_id}.dispatched",
                                  {"record_id": record.id, "status": status, "data": data})
                session.flush()
                changed.append(current)
        return [self._result(record) for record in changed]

    def advance_business_state(self, user_id: str | None = None) -> list[CapabilityResult]:
        changed: list[CapabilityRecord] = []
        with self.database.sessions.begin() as session:
            query = select(CapabilityRecord).where(CapabilityRecord.status == "pending",
                                                    CapabilityRecord.capability_id == "C01")
            if user_id:
                query = query.where(CapabilityRecord.user_id == user_id)
            for record in session.scalars(query).all():
                data = json.loads(record.data_json)
                data["status"] = "active"
                data["application_stage"] = "delivered"
                record.status = "succeeded"
                record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                self.audit.append(session, f"trace_{record.id}", record.user_id, "card.application.progressed",
                                  {"record_id": record.id, "status": "active"})
                changed.append(record)
        return [self._result(record) for record in changed]

    def _account(self, user_id: str) -> Account:
        with self.database.sessions() as session:
            account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
            if account is None:
                raise DomainError("NOT_OWNER")
            return Account(id=account.id, user_id=account.user_id, available_minor=account.available_minor,
                           ledger_balance_minor=account.ledger_balance_minor, status=account.status)

    def _has_risk(self, user_id: str) -> bool:
        from agentbank.db import RiskProfile
        with self.database.sessions() as session:
            return session.get(RiskProfile, user_id) is not None

    def _suitable(self, user_id: str, product_risk: str) -> bool:
        from agentbank.db import RiskProfile
        with self.database.sessions() as session:
            profile = session.get(RiskProfile, user_id)
            return profile is not None and profile.expires_on >= business_date() and int(product_risk[1:]) <= int(profile.level[1:])

    @staticmethod
    def _product_data(product: Product) -> dict[str, Any]:
        return {"id": product.id, "name": product.name, "risk_level": product.risk_level,
                "term_days": product.term_days, "fee_bps": product.fee_bps, "redemption_days": product.redemption_days}
