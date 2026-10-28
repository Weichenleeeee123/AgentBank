from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agentbank.clock import utc_now_naive


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class AuditEventRecord:
    seq: int
    event_id: str
    trace_id: str
    subject_id: str
    event_type: str
    payload: dict[str, Any]
    previous_hash: str | None
    event_hash: str
    occurred_at: datetime


@dataclass
class AuditChain:
    events: list[AuditEventRecord] = field(default_factory=list)

    def append(self, trace_id: str, subject_id: str, event_type: str, payload: dict[str, Any]) -> AuditEventRecord:
        previous = self.events[-1].event_hash if self.events else None
        seq = len(self.events) + 1
        event_id = f"aud_{uuid.uuid4().hex}"
        occurred_at = utc_now_naive()
        body = {"seq": seq, "event_id": event_id, "trace_id": trace_id, "subject_id": subject_id,
                "event_type": event_type, "payload": payload, "previous_hash": previous,
                "occurred_at": occurred_at.isoformat()}
        digest = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        record = AuditEventRecord(seq, event_id, trace_id, subject_id, event_type, payload, previous, digest, occurred_at)
        self.events.append(record)
        return record

    def verify(self) -> bool:
        previous: str | None = None
        for index, event in enumerate(self.events, start=1):
            if event.seq != index or event.previous_hash != previous:
                return False
            body = {"seq": event.seq, "event_id": event.event_id, "trace_id": event.trace_id,
                    "subject_id": event.subject_id, "event_type": event.event_type, "payload": event.payload,
                    "previous_hash": event.previous_hash, "occurred_at": event.occurred_at.isoformat()}
            expected = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
            if expected != event.event_hash:
                return False
            previous = event.event_hash
        return True
