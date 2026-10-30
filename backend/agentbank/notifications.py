from __future__ import annotations

import json
import uuid
from typing import Callable

from sqlalchemy import func, select

from agentbank.db import Database, DeliveryAttempt, OutboxEvent, UserNotification


class OutboxDeliveryService:
    """Delivers pending outbox events while recording bounded, retryable attempts."""

    def __init__(self, database: Database, deliver: Callable[[dict[str, str]], None] | None = None,
                 *, max_attempts: int = 5) -> None:
        self.database = database
        self.deliver_callback = deliver or (lambda _event: None)
        self.max_attempts = max_attempts

    def deliver(self, user_id: str | None = None) -> list[dict[str, str]]:
        with self.database.sessions() as session:
            query = select(OutboxEvent).where(OutboxEvent.status == "pending")
            if user_id:
                query = query.where(OutboxEvent.user_id == user_id)
            events = [(event.id, event.user_id, event.event_type, event.dedupe_key, event.payload_json)
                      for event in session.scalars(query.order_by(OutboxEvent.created_at)).all()]

        results: list[dict[str, str]] = []
        for event_id, owner, event_type, dedupe_key, payload_json in events:
            with self.database.sessions() as session:
                attempts = int(session.scalar(select(func.count()).select_from(DeliveryAttempt).where(
                    DeliveryAttempt.outbox_id == event_id)) or 0)
            try:
                self.deliver_callback({"event_id": event_id, "user_id": owner, "event_type": event_type,
                                       "dedupe_key": dedupe_key, **json.loads(payload_json)})
            except Exception as error:
                attempt_number = attempts + 1
                with self.database.sessions.begin() as session:
                    event = session.get(OutboxEvent, event_id)
                    if event is None or event.status != "pending":
                        continue
                    exhausted = attempt_number >= self.max_attempts
                    session.add(DeliveryAttempt(id=f"attempt_{uuid.uuid4().hex}", outbox_id=event.id,
                                                attempt_number=attempt_number,
                                                status="exhausted" if exhausted else "failed",
                                                error=f"{type(error).__name__}: {error}"[:1000]))
                    if exhausted:
                        event.status = "failed"
                    results.append({"event_id": event_id, "dedupe_key": dedupe_key,
                                    "status": "failed" if exhausted else "retry_pending"})
                continue

            with self.database.sessions.begin() as session:
                event = session.get(OutboxEvent, event_id)
                if event is None or event.status != "pending":
                    continue
                session.add(DeliveryAttempt(id=f"attempt_{uuid.uuid4().hex}", outbox_id=event.id,
                                            attempt_number=attempts + 1, status="delivered"))
                if session.scalar(select(UserNotification).where(UserNotification.dedupe_key == event.dedupe_key)) is None:
                    payload = json.loads(event.payload_json)
                    session.add(UserNotification(
                        id=f"notice_{uuid.uuid4().hex}", user_id=event.user_id, event_type=event.event_type,
                        title=str(payload.get("title", "订阅续费提醒")),
                        body=str(payload.get("body", f"{payload.get('merchant', '订阅')}将在{payload.get('charge_date', '')}续费。")),
                        dedupe_key=event.dedupe_key, status="unread"))
                event.status = "delivered"
                results.append({"event_id": event_id, "dedupe_key": dedupe_key, "status": "delivered"})
        return results
