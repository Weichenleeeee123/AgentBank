from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.errors import DomainError


@dataclass(frozen=True)
class FeishuEvent:
    event_id: str
    user_id: str
    text: str
    conversation_id: str


class FeishuAdapter:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.seen_events: set[str] = set()

    def verify_signature(self, payload: dict[str, Any], signature: str | None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        expected = hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise DomainError("IM_SIGNATURE_INVALID")

    def parse(self, payload: dict[str, Any]) -> FeishuEvent:
        event_id = str(payload.get("event_id", ""))
        user_id = str(payload.get("user_id", ""))
        text = str(payload.get("text", ""))
        if not event_id or not user_id:
            raise DomainError("IM_EVENT_INVALID")
        if event_id in self.seen_events:
            raise DomainError("WEBIM_REPLAYED")
        self.seen_events.add(event_id)
        return FeishuEvent(event_id, user_id, text, f"feishu-{event_id}")

    def handle(self, payload: dict[str, Any], signature: str | None, agent: AgentOrchestrator) -> dict[str, Any]:
        self.verify_signature(payload, signature)
        event = self.parse(payload)
        from agentbank.db import WebImEvent
        with agent.database.atomic() as session:
            event_key = f"feishu:{event.event_id}"
            if session.get(WebImEvent, event_key) is not None:
                raise DomainError("WEBIM_REPLAYED")
            session.add(WebImEvent(event_id=event_key, user_id=event.user_id, event_type="feishu"))
        response = agent.handle(event.user_id, event.conversation_id, event.text, "feishu")
        if bool(payload.get("group", False)):
            return {"event_id": event.event_id, "type": "generic_status", "message": "请在私聊或 App 查看结果。"}
        return {"event_id": event.event_id, "type": response.kind, **response.model_dump()}
