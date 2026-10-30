from __future__ import annotations

import json
import uuid
from typing import Any
from datetime import date

from sqlalchemy import select

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.analysis.insights import birthday_history_clues
from agentbank.db import Event, Transaction, UserNotification
from agentbank.notifications import OutboxDeliveryService


class BusinessScheduler:
    """One idempotent scheduler tick for due business work in one app instance."""

    def __init__(self, capabilities: CapabilityService,
                 notification_delivery: OutboxDeliveryService | None = None) -> None:
        self.capabilities = capabilities
        self.notification_delivery = notification_delivery or OutboxDeliveryService(capabilities.database)

    def tick(self) -> dict[str, Any]:
        orders = self.capabilities.dispatch_due()
        investments = self.capabilities.progress_investments(due_only=True)
        reminders = self.capabilities.dispatch_reminders()
        delivered = self.notification_delivery.deliver()
        birthdays = self.discover_birthdays()
        cards = self.capabilities.advance_business_state()
        return {"orders_dispatched": len(orders), "investments_settled": len(investments),
                "reminders_scheduled": len(reminders), "reminders_delivered": len(delivered),
                "birthday_recommendations": len(birthdays), "cards_progressed": len(cards)}

    def discover_birthdays(self) -> list[dict[str, Any]]:
        today = date.fromisoformat(self.capabilities.clock.business_date())
        created: list[dict[str, Any]] = []
        with self.capabilities.database.sessions() as session:
            events = [(event.id, event.user_id, event.title, event.event_date)
                      for event in session.scalars(select(Event)).all()]
        for event_id, user_id, title, event_date in events:
            try:
                target = date.fromisoformat(event_date)
            except ValueError:
                continue
            if (target - today).days != 30:
                continue
            clues = birthday_history_clues(self.capabilities.database, user_id, event_date)
            if not clues:
                continue
            with self.capabilities.database.sessions() as session:
                evidence_ids = [txn_id for clue in clues for txn_id in clue["txn_ids"]]
                transactions = session.scalars(select(Transaction).where(Transaction.id.in_(evidence_ids))).all()
                prior_year_ids = sorted(txn.id for txn in transactions
                                        if txn.posted_at[:4] == str(target.year - 1))
            if not prior_year_ids:
                continue
            dedupe_key = f"birthday-recommendation:{event_id}:{target.isoformat()}"
            with self.capabilities.database.sessions.begin() as session:
                if session.scalar(select(UserNotification).where(UserNotification.dedupe_key == dedupe_key)) is not None:
                    continue
                event = session.get(Event, event_id)
                if event is None:
                    continue
                session.add(UserNotification(
                    id=f"notice_{uuid.uuid4().hex}", user_id=user_id,
                    event_type="birthday.recommendation", title="生日礼物建议",
                    body=json.dumps({"recommendation_only": True, "event_title": title,
                                     "event_date": target.isoformat(), "evidence_txn_ids": prior_year_ids},
                                    ensure_ascii=False, sort_keys=True),
                    dedupe_key=dedupe_key, status="unread"))
                created.append({"event_id": event_id, "dedupe_key": dedupe_key,
                                "recommendation_only": True, "evidence": prior_year_ids})
        return created
