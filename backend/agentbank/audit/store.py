from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentbank.audit.chain import _canonical
from agentbank.clock import utc_now_naive
from agentbank.db import AuditCheckpoint, AuditLog, Database


class PersistentAudit:
    def __init__(self, database: Database) -> None:
        self.database = database

    def append(self, session: Session, trace_id: str, subject_id: str, event_type: str,
               payload: dict[str, Any]) -> AuditLog:
        previous = session.scalar(select(AuditLog).order_by(AuditLog.seq.desc()))
        previous_hash = previous.event_hash if previous else None
        event_id = f"aud_{uuid.uuid4().hex}"
        occurred_at = utc_now_naive()
        seq = (previous.seq + 1) if previous else 1
        body = {"seq": seq, "event_id": event_id, "trace_id": trace_id, "subject_id": subject_id,
                "event_type": event_type, "payload": payload, "previous_hash": previous_hash,
                "occurred_at": occurred_at.isoformat()}
        import hashlib

        event_hash = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        record = AuditLog(seq=seq, event_id=event_id, trace_id=trace_id, subject_id=subject_id,
                          event_type=event_type, payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                          previous_hash=previous_hash, event_hash=event_hash, occurred_at=occurred_at)
        session.add(record)
        return record

    def verify(self) -> bool:
        with self.database.sessions() as session:
            records = session.scalars(select(AuditLog).order_by(AuditLog.seq)).all()
            previous_hash: str | None = None
            expected_seq = 1
            for record in records:
                if record.seq != expected_seq:
                    return False
                body = {"seq": record.seq, "event_id": record.event_id, "trace_id": record.trace_id,
                        "subject_id": record.subject_id, "event_type": record.event_type,
                        "payload": json.loads(record.payload_json), "previous_hash": record.previous_hash,
                        "occurred_at": record.occurred_at.isoformat()}
                import hashlib

                expected = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
                if record.previous_hash != previous_hash or expected != record.event_hash:
                    return False
                previous_hash = record.event_hash
                expected_seq += 1
            checkpoints = session.scalars(select(AuditCheckpoint).order_by(AuditCheckpoint.seq)).all()
            for checkpoint in checkpoints:
                matching = next((record for record in records if record.seq == checkpoint.seq), None)
                if matching is None or matching.event_hash != checkpoint.event_hash:
                    return False
            return True

    def checkpoint(self) -> AuditCheckpoint | None:
        with self.database.sessions.begin() as session:
            record = session.scalar(select(AuditLog).order_by(AuditLog.seq.desc()))
            if record is None:
                return None
            checkpoint = AuditCheckpoint(id=f"chk_{uuid.uuid4().hex}", seq=record.seq,
                                         event_hash=record.event_hash)
            session.add(checkpoint)
            return checkpoint
