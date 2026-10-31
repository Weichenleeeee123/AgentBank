from __future__ import annotations

import json
import uuid
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentbank.audit.chain import _canonical
from agentbank.clock import utc_now_naive
from agentbank.db import AuditCheckpoint, AuditLog, Database
from agentbank.errors import DomainError
from agentbank.security.dlp import redact_payload


class PersistentAudit:
    def __init__(self, database: Database) -> None:
        self.database = database
        filename = database.engine.url.database
        self.checkpoint_path: Path | None = None
        if filename and filename != ":memory:":
            self.checkpoint_path = Path(str(Path(filename).resolve()) + ".checkpoints.jsonl")
        self.key = os.getenv("AGENTBANK_AUDIT_SECRET", "")
        if not self.key:
            if self.checkpoint_path is not None:
                key_path = self.checkpoint_path.with_suffix(".key")
                try:
                    with key_path.open("x", encoding="utf-8") as stream:
                        stream.write(secrets.token_urlsafe(48))
                    try:
                        key_path.chmod(0o600)
                    except OSError:
                        pass
                except FileExistsError:
                    pass
                self.key = key_path.read_text(encoding="utf-8")
            else:
                self.key = "in-memory-simulation"

    def append(self, session: Session, trace_id: str, subject_id: str, event_type: str,
               payload: dict[str, Any]) -> AuditLog:
        payload = redact_payload(payload)
        previous = session.scalar(select(AuditLog).order_by(AuditLog.seq.desc()))
        previous_hash = previous.event_hash if previous else None
        event_id = f"aud_{uuid.uuid4().hex}"
        occurred_at = utc_now_naive()
        seq = (previous.seq + 1) if previous else 1
        body = {"seq": seq, "event_id": event_id, "trace_id": trace_id, "subject_id": subject_id,
                "event_type": event_type, "payload": payload, "previous_hash": previous_hash,
                "occurred_at": occurred_at.isoformat()}

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
            if self.checkpoint_path is not None and self.checkpoint_path.exists():
                try:
                    by_seq = {record.seq: record.event_hash for record in records}
                    for line in self.checkpoint_path.read_text(encoding="utf-8").splitlines():
                        anchor = json.loads(line)
                        signature = str(anchor.pop("signature"))
                        expected_signature = hmac.new(self.key.encode(), _canonical(anchor).encode(), hashlib.sha256).hexdigest()
                        if not hmac.compare_digest(signature, expected_signature):
                            return False
                        if by_seq.get(anchor["last_seq"]) != anchor["head_hash"]:
                            return False
                except (ValueError, KeyError, OSError, TypeError):
                    return False
            return True

    def checkpoint(self) -> AuditCheckpoint | None:
        with self.database.atomic() as session:
            record = session.scalar(select(AuditLog).order_by(AuditLog.seq.desc()))
            if record is None:
                return None
            checkpoint = session.scalar(select(AuditCheckpoint).where(AuditCheckpoint.seq == record.seq))
            if checkpoint is not None:
                if checkpoint.event_hash != record.event_hash:
                    raise DomainError("AUDIT_UNAVAILABLE")
                return checkpoint
            checkpoint = AuditCheckpoint(id=f"chk_{uuid.uuid4().hex}", seq=record.seq,
                                         event_hash=record.event_hash, created_at=utc_now_naive())
            session.add(checkpoint)
            session.flush()
            if self.checkpoint_path is not None:
                anchor = {"last_seq": record.seq, "head_hash": record.event_hash,
                          "ts": checkpoint.created_at.isoformat()}
                signature = hmac.new(self.key.encode(), _canonical(anchor).encode(), hashlib.sha256).hexdigest()
                try:
                    with self.checkpoint_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({**anchor, "signature": signature}, sort_keys=True) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError as error:
                    raise DomainError("AUDIT_UNAVAILABLE") from error
            return checkpoint

    def checkpoint_and_verify(self) -> AuditCheckpoint | None:
        if not self.verify():
            raise DomainError("AUDIT_UNAVAILABLE")
        checkpoint = self.checkpoint()
        if not self.verify():
            raise DomainError("AUDIT_UNAVAILABLE")
        return checkpoint
