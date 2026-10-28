from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from agentbank.analysis.dsl import DslExecutor
from agentbank.analysis.insights import detect_anomalies, detect_subscriptions, monthly_report
from agentbank.audit.store import PersistentAudit
from agentbank.bank.business import CardService, BirthdayPlanner, MandateService, RecipientResolver, RiskService, SubscriptionService
from agentbank.bank.capabilities import split_bill
from agentbank.bank.service import TransferService
from agentbank.clock import BusinessClock
from agentbank.contracts import CapabilityResult
from agentbank.db import Account, Card, CapabilityRecord, Database, Product, Transaction
from agentbank.errors import DomainError
from agentbank.security.breaker import CircuitBreaker
from agentbank.tools import CAPABILITIES


class CapabilityService:
    """Deterministic handlers for every M2 capability ID.

    The service stores a business record for every command/query invocation so the
    evaluator can assert state and audit evidence instead of only observing a tool call.
    """

    def __init__(self, database: Database, clock: BusinessClock | None = None) -> None:
        self.database = database
        self.clock = clock or BusinessClock()
        self.audit = PersistentAudit(database)
        self.breaker = CircuitBreaker(database)

    def execute(self, user_id: str, capability_id: str, payload: dict[str, Any], idempotency_key: str) -> CapabilityResult:
        if capability_id not in CAPABILITIES:
            raise DomainError("CAPABILITY_NOT_REGISTERED")
        if capability_id in {"W04", "W05", "C04", "C05", "C06", "C08"} or (capability_id == "C01" and not bool(payload.get("virtual", True))):
            if payload.get("assurance") != "mfa":
                raise DomainError("MFA_REQUIRED")
        read_only = capability_id in {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "S01", "X01"}
        protective = capability_id in {"C02", "C03", "C07", "X05"}
        if not read_only and not protective and self.breaker.is_locked(user_id):
            with self.database.sessions.begin() as session:
                self.audit.append(session, f"trace_security_{uuid.uuid4().hex}", user_id, "security.breaker.blocked",
                                  {"capability_id": capability_id, "reason": "U17"})
            raise DomainError("SECURITY_LOCKED")
        with self.database.sessions() as session:
            existing = session.scalar(select(CapabilityRecord).where(CapabilityRecord.idempotency_key == idempotency_key))
            if existing is not None:
                if existing.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                return self._result(existing)
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

    def get(self, user_id: str, record_id: str) -> CapabilityResult:
        with self.database.sessions() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
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
            return {"recipient_id": candidate.id, "masked_name": candidate.masked_name, "phone_tail": candidate.phone_tail,
                    "is_new": candidate.is_new, "next": "transfer.create"}, "prepared"
        if capability_id == "T01":
            prepared = TransferService(self.database).prepare(user_id, str(payload.get("recipient_name", "")),
                                                              int(payload.get("amount_minor", 0)), payload.get("memo"))
            return {"plan_id": prepared.plan_id, "receipt_id": prepared.receipt_id, "display_hash": prepared.display_hash,
                    "action_hash": prepared.action_hash, "tier_display": prepared.tier_display,
                    "assurance": prepared.assurance.value, "display": prepared.display}, "prepared"
        if capability_id == "T04":
            period = str(payload.get("period", "monthly"))
            return {"schedule_id": f"schedule_{uuid.uuid4().hex}", "period": period, "next_due": payload.get("next_due", self.clock.business_date()),
                    "max_uses": int(payload.get("max_uses", 1)), "operation_template": f"op_{idempotency_key}"}, "scheduled"
        if capability_id == "T05":
            participants = [str(item) for item in payload.get("participant_ids", [user_id, "user_002", "user_003"])]
            return {"shares": split_bill(int(payload.get("total_minor", 10_000)), participants, user_id),
                    "participant_count": len(participants), "request_only": True}, "created"
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
            products = RiskService(self.database).recommend(user_id) if self._has_risk(user_id) else []
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
            return {"order_id": f"order_{uuid.uuid4().hex}", "product_id": product_id, "status": "subscribed",
                    "shares": amount_minor // 100, "amount_minor": amount_minor, "funds_status": "debited"}, "succeeded"
        if capability_id == "W05":
            return {"order_id": str(payload.get("order_id", f"order_{uuid.uuid4().hex}")), "status": "processing",
                    "expected_settlement_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}, "processing"
        if capability_id == "W06":
            with self.database.sessions() as session:
                orders = session.scalars(select(CapabilityRecord).where(CapabilityRecord.user_id == user_id,
                                                                        CapabilityRecord.capability_id == "W04",
                                                                        CapabilityRecord.status == "succeeded")).all()
            return {"positions": [json.loads(order.data_json) for order in orders],
                    "source": "simulated_bank", "as_of": datetime.now(timezone.utc).isoformat()}, "succeeded"
        if capability_id.startswith("C"):
            return self._card(user_id, capability_id, payload)
        if capability_id == "S01":
            return {"subscriptions": detect_subscriptions(self.database, user_id)}, "succeeded"
        if capability_id == "S02":
            return {"reminder_id": f"rem_{uuid.uuid4().hex}", "remind_before_days": int(payload.get("days", 3)), "dedupe_key": idempotency_key}, "scheduled"
        if capability_id == "S03":
            subscription_id = str(payload.get("subscription_id", "sub_video"))
            SubscriptionService(self.database).cancel(user_id, subscription_id)
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
            return {"quote_id": f"quote_{uuid.uuid4().hex}", "order_id": f"merchant_order_{uuid.uuid4().hex}",
                    "item": "flower" if capability_id == "X03" else "cake", "status": "scheduled",
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
            with self.database.sessions.begin() as session:
                session.add(Card(id=card_id, user_id=user_id, status="active" if payload.get("virtual", True) else "pending"))
            return {"card_id": card_id, "status": "active" if payload.get("virtual", True) else "pending", "card_type": "virtual" if payload.get("virtual", True) else "physical"}, "succeeded"
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

    def dispatch_due(self, user_id: str | None = None) -> list[CapabilityResult]:
        today = self.clock.business_date()
        changed: list[CapabilityRecord] = []
        with self.database.sessions.begin() as session:
            query = select(CapabilityRecord).where(CapabilityRecord.status == "scheduled",
                                                    CapabilityRecord.capability_id.in_(["T04", "X03", "X04"]))
            if user_id:
                query = query.where(CapabilityRecord.user_id == user_id)
            records = list(session.scalars(query).all())
            for record in records:
                data = json.loads(record.data_json)
                due = str(data.get("next_due") or data.get("delivery_date") or "9999-12-31")
                if due > today:
                    continue
                if record.capability_id == "T04" and int(data.get("max_uses", 1)) > 1:
                    data["max_uses"] = int(data["max_uses"]) - 1
                    operation_ids = list(data.get("operation_ids", []))
                    operation_ids.append(f"{record.id}-period-{len(operation_ids) + 1}")
                    data["operation_ids"] = operation_ids
                    record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                    if data["max_uses"] <= 0:
                        record.status = "succeeded"
                else:
                    data["operation_id"] = f"{record.id}-operation"
                    data["status"] = "succeeded"
                    record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                    record.status = "succeeded"
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
            return profile is not None and int(product_risk[1:]) <= int(profile.level[1:])

    @staticmethod
    def _product_data(product: Product) -> dict[str, Any]:
        return {"id": product.id, "name": product.name, "risk_level": product.risk_level,
                "term_days": product.term_days, "fee_bps": product.fee_bps, "redemption_days": product.redemption_days}
