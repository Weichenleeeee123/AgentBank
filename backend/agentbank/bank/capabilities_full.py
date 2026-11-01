from __future__ import annotations

import json
import hashlib
import secrets
import hmac
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agentbank.analysis.dsl import DslExecutor
from agentbank.analysis.insights import birthday_history_clues, detect_anomalies, detect_subscriptions, monthly_report, yearly_report
from agentbank.audit.store import PersistentAudit
from agentbank.auth.devices import DeviceService
from agentbank.bank.business import CardService, BirthdayPlanner, MandateService, RecipientResolver, RiskService, SubscriptionService
from agentbank.bank.capabilities import split_bill
from agentbank.bank.service import TransferService
from agentbank.bank.ledger import post_payment, post_redemption, require_active, require_payment, require_safe_risk
from agentbank.clock import BusinessClock, utc_now_naive, next_scheduled_date
from agentbank.contracts import CapabilityResult
from agentbank.db import AABill, AAShare, Account, Card, CardApplication, CapabilityRecord, Database, DebitMandate, InvestmentOrder, Mandate, MerchantOrder, Operation, OutboxEvent, Payee, Position, Product, ReceiptConsumption, ReceiptRecord, Reminder, Reservation, ScheduledJob, Subscription, Transaction, UserNotification
from agentbank.db import MerchantCatalog, MerchantQuote
from agentbank.db import RiskContextRecord, PendingPinChange
from agentbank.db import PlanNodeRecord, PlanRecord
from agentbank.contracts import Assurance
from agentbank.policy.engine import PolicyContext, decide
from agentbank.bank.questionnaire import VERSION, answer_display
from agentbank.errors import DomainError
from agentbank.security.breaker import CircuitBreaker
from agentbank.tools import CAPABILITIES, validate_capability_payload


class CapabilityService:
    """Deterministic handlers for every M2 capability ID.

    The service stores a business record for every command/query invocation so the
    evaluator can assert state and audit evidence instead of only observing a tool call.
    """

    def __init__(self, database: Database, clock: BusinessClock | None = None, *, require_device_mfa: bool = False, enforce_prepare: bool = False) -> None:
        self.database = database
        if clock is not None:
            database.business_clock = clock
        self.clock = database.business_clock
        self.audit = PersistentAudit(database)
        self.breaker = CircuitBreaker(database)
        self.device_tokens: dict[str, str] = {}
        self.require_device_mfa = require_device_mfa
        self.enforce_prepare = enforce_prepare

    def execute(self, user_id: str, capability_id: str, payload: dict[str, Any], idempotency_key: str,
                *, trusted_execution: bool = False) -> CapabilityResult:
        if capability_id not in CAPABILITIES:
            raise DomainError("CAPABILITY_NOT_REGISTERED")
        # Execution-only authorization markers are added by the trusted plan
        # executor after confirmation.  They control the security path but are
        # not part of the public capability input contract.
        internal_markers = {"_confirmed", "_mfa_verified"}
        if not trusted_execution and internal_markers.intersection(payload):
            raise DomainError("CAPABILITY_INPUT_UNKNOWN_FIELD")
        public_payload = {key: value for key, value in payload.items()
                          if key not in internal_markers}
        validate_capability_payload(capability_id, public_payload)
        request_hash = "sha256:" + hashlib.sha256(json.dumps(self._safe_payload(capability_id, payload), ensure_ascii=False, sort_keys=True,
                                                              separators=(",", ":")).encode("utf-8")).hexdigest()
        with self.database.sessions() as session:
            existing = session.scalar(select(CapabilityRecord).where(CapabilityRecord.idempotency_key == idempotency_key))
            if existing is not None:
                if existing.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                if existing.request_hash and existing.request_hash != request_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT")
                if capability_id == 'C08':
                    pending = session.get(PendingPinChange, idempotency_key)
                    if pending is None or not self._pin_matches(str(payload.get('new_pin', '')), pending.pin_hash):
                        raise DomainError('IDEMPOTENCY_CONFLICT')
                return self._result(existing)
        payload = dict(payload)
        if capability_id == 'W03':
            if payload.get('questionnaire_version', VERSION) != VERSION:
                raise DomainError('QUESTIONNAIRE_VERSION_INVALID')
            payload['questionnaire_version'] = VERSION
        if capability_id == 'T05' and payload.get('action') == 'pay_share':
            with self.database.atomic() as session:
                payload.update(self._aa_snapshot(session, user_id, str(payload['bill_id'])))
        if capability_id == 'C08':
            with self.database.sessions() as session:
                owned_card = session.get(Card, str(payload.get('card_id', 'card_001')))
                if owned_card is None or owned_card.user_id != user_id:
                    raise DomainError('NOT_OWNER')
            pin = payload.get('new_pin')
            if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit() or not 4 <= len(pin) <= 6:
                raise DomainError('PIN_POLICY_INVALID')
            with self.database.atomic() as session:
                pending_pin = session.get(PendingPinChange, idempotency_key)
                if pending_pin is None:
                    session.add(PendingPinChange(id=idempotency_key, user_id=user_id,
                        card_id=str(payload.get('card_id', 'card_001')), pin_hash=self._hash_pin(pin),
                        expires_at=utc_now_naive() + timedelta(minutes=5)))
                elif (pending_pin.user_id != user_id or pending_pin.card_id != str(payload.get('card_id', 'card_001'))
                      or not self._pin_matches(pin, pending_pin.pin_hash)):
                    raise DomainError('IDEMPOTENCY_CONFLICT')
        if capability_id in {"X03", "X04"}:
            item = "flower" if capability_id == "X03" else "cake"
            delivery_date = str(payload.get("delivery_date", self.clock.business_date()))
            quote_id = payload.get("quote_id")
            if quote_id:
                quote = self.get_merchant_quote(user_id, str(quote_id))
                if quote["item"] != item or quote["delivery_date"] != delivery_date:
                    raise DomainError("QUOTE_MISMATCH")
                if quote["status"] != "active" or datetime.fromisoformat(quote["expires_at"]) <= utc_now_naive():
                    raise DomainError("QUOTE_EXPIRED")
                if "amount_minor" in payload and int(payload["amount_minor"]) != quote["amount_minor"]:
                    raise DomainError("QUOTE_AMOUNT_MISMATCH")
                payload["amount_minor"] = quote["amount_minor"]
            else:
                quote = self.quote_merchant(user_id, item, delivery_date,
                                            amount_minor=int(payload["amount_minor"]) if "amount_minor" in payload else None)
                payload["quote_id"] = quote["quote_id"]
                payload["amount_minor"] = quote["amount_minor"]
        validate_capability_payload(capability_id, public_payload)
        read_only_capabilities = {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "X01"}
        write_capability = capability_id not in read_only_capabilities
        if capability_id == "T05" and payload.get("action") in {"preview", "status"}:
            write_capability = False
        if self.enforce_prepare and write_capability and not payload.get("_confirmed") and not payload.get("_mfa_verified"):
            if capability_id in {"T01", "T02", "T03"}:
                prepared, _ = self._dispatch(user_id, capability_id, payload, idempotency_key)
                action_digest = str(prepared["action_hash"])
                display_digest = str(prepared["display_hash"])
                assurance = str(prepared["assurance"])
            else:
                action_digest, display_digest = self._payload_hashes(capability_id, payload)
                red = capability_id in {"W04", "W05", "C04", "C05", "C06", "C08"} or (capability_id == "C01" and not bool(payload.get("virtual", True))) or (capability_id == "C07" and payload.get("action") == "unfreeze")
                if capability_id == "C02":
                    with self.database.sessions() as session:
                        card = session.get(Card, str(payload.get("card_id", "card_001")))
                        red = card is not None and int(payload.get("limit_minor", card.limit_minor)) > card.limit_minor
                if capability_id == "C07" and payload.get("action") == "unfreeze":
                    red = True
                if capability_id == 'T05' and payload.get('action') == 'pay_share':
                    with self.database.sessions() as session:
                        red = self._requires_mfa(session, user_id, capability_id, payload)
                assurance = "mfa" if red else "confirm"
            record_id = f"cap_{uuid.uuid4().hex}"
            record_status = "awaiting_confirmation"
            data: dict[str, Any] = {"capability_id": capability_id, "payload": self._safe_payload(capability_id, payload), "action_hash": action_digest,
                    "display_hash": display_digest, "assurance": assurance}
            if capability_id == 'C08':
                data['display'] = self._pin_display(payload)
            elif capability_id not in {'T01', 'T02', 'T03'}:
                data['display'] = self._capability_display(capability_id, payload)
            if capability_id in {"T01", "T02", "T03"}:
                data.update(prepared)
                data["capability_id"] = capability_id
                data["payload"] = payload
                record_status = "awaiting_mfa" if assurance == "mfa" else "awaiting_confirmation"
            with self.database.sessions.begin() as session:
                receipt_id = str(data.get("receipt_id") or f"rcp_{uuid.uuid4().hex}")
                session.add(CapabilityRecord(id=record_id, capability_id=capability_id, user_id=user_id,
                                             status=record_status, data_json=json.dumps(data, ensure_ascii=False),
                                             idempotency_key=idempotency_key, request_hash=request_hash))
                if capability_id not in {"T01", "T02", "T03"}:
                    session.add(ReceiptRecord(id=receipt_id, subject_id=user_id, plan_id=record_id, plan_revision=1,
                                             node_ids_json=json.dumps([record_id]), action_hash=action_digest,
                                             display_hash=display_digest, assurance=assurance, issued_at=utc_now_naive(),
                                             expires_at=utc_now_naive() + timedelta(minutes=5), status="pending"))
                self.audit.append(session, f"trace_{record_id}", user_id, f"capability.{capability_id}.prepared",
                                  {"record_id": record_id, "receipt_id": receipt_id, "action_hash": action_digest,
                                   "display_hash": display_digest, "assurance": assurance})
            data["receipt_id"] = receipt_id
            with self.database.sessions.begin() as session:
                record = session.get(CapabilityRecord, record_id)
                if record is not None:
                    record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
            return CapabilityResult(capability_id=capability_id, record_id=record_id, status=record_status,
                                    data=data, evidence={"audit_event": f"capability.{capability_id}.prepared",
                                                         "idempotency_key": idempotency_key})
        red_capability = capability_id in {"W04", "W05", "C04", "C05", "C06", "C08"} or (capability_id == "C01" and not bool(payload.get("virtual", True)))
        if capability_id == "C02":
            with self.database.sessions() as session:
                card_for_policy = session.get(Card, str(payload.get("card_id", "card_001")))
                red_capability = card_for_policy is not None and int(payload.get("limit_minor", card_for_policy.limit_minor)) > card_for_policy.limit_minor
        if capability_id == "C07" and payload.get("action") == "unfreeze":
            red_capability = True
        if red_capability:
            if payload.get("assurance") != "mfa":
                raise DomainError("MFA_REQUIRED")
            device_token = payload.get("device_token")
            otp = payload.get("otp")
            security_payload = {key: value for key, value in payload.items()
                               if key not in {"device_token", "otp", "assurance"}}
            if capability_id == "X05":
                expected_action, expected_display = self._loss_report_hashes(
                    capability_id, str(idempotency_key), str(security_payload.get("card_id", "card_001"))
                )
            else:
                expected_action, expected_display = self._payload_hashes(capability_id, security_payload)
            if self.require_device_mfa and not payload.get("_mfa_verified") and not (isinstance(device_token, str) and isinstance(otp, str)):
                record_id = f"cap_{uuid.uuid4().hex}"
                pending_data: dict[str, Any] = {"payload": self._safe_payload(capability_id, security_payload), "assurance": "mfa",
                                "action_hash": expected_action, "display_hash": expected_display,
                                "challenge_record_id": record_id}
                if capability_id == 'C08':
                    pending_data['display'] = self._pin_display(payload)
                with self.database.sessions.begin() as session:
                    session.add(CapabilityRecord(id=record_id, capability_id=capability_id, user_id=user_id,
                                                 status="awaiting_mfa", data_json=json.dumps(pending_data, ensure_ascii=False),
                                                 idempotency_key=idempotency_key, request_hash=request_hash))
                if user_id in self.device_tokens:
                    DeviceService(self.database).issue_otp(user_id, record_id, expected_action)
                return CapabilityResult(capability_id=capability_id, record_id=record_id, status="awaiting_mfa",
                                        data=pending_data, evidence={"audit_event": f"capability.{capability_id}.awaiting_mfa",
                                                                       "idempotency_key": idempotency_key})
            if self.require_device_mfa and not payload.get("_mfa_verified"):
                DeviceService(self.database).validate_token(user_id, str(device_token))
                DeviceService(self.database).verify(user_id, str(otp), None, expected_action)
        if capability_id == "C02":
            with self.database.sessions() as session:
                card = session.get(Card, str(payload.get("card_id", "card_001")))
                if card is not None and int(payload.get("limit_minor", card.limit_minor)) > card.limit_minor and payload.get("assurance") != "mfa":
                    raise DomainError("MFA_REQUIRED")
        if capability_id == "C07" and payload.get("action") == "unfreeze" and payload.get("assurance") != "mfa":
            raise DomainError("MFA_REQUIRED")
        read_only = capability_id in {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "X01"} or (capability_id == "T05" and payload.get("action") in {"preview", "status"})
        with self.database.sessions() as policy_session:
            protective = self._is_protective(policy_session, capability_id, payload)
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
                                      idempotency_key=idempotency_key, request_hash=request_hash)
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
        receipt_id = str(data.get("receipt_id") or record_id)
        capability_id = record.capability_id
        payload = dict(data["payload"])
        if capability_id not in {"T01", "T02", "T03"}:
            if capability_id == "X05":
                expected_action, expected_display = self._loss_report_hashes(
                    capability_id, record_id, str(payload.get("card_id", "card_001"))
                )
            else:
                expected_action, expected_display = self._payload_hashes(capability_id, payload)
            if capability_id != 'X05' and 'display' in data:
                expected_display = self._payload_display_hash(expected_action, data.get('display', []))
            if data.get("action_hash") != expected_action or data.get("display_hash") != expected_display:
                raise DomainError("HASH_MISMATCH")
        DeviceService(self.database).verify(user_id, code, receipt_id, str(data.get("action_hash", "")) or None)
        with self.database.atomic():
            session = self.database.sessions()
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id or record.status != "awaiting_mfa":
                raise DomainError("RECEIPT_REPLAYED")
            if record.capability_id != capability_id:
                raise DomainError("HASH_MISMATCH")
            data = json.loads(record.data_json)
            if str(data.get("receipt_id") or record_id) != receipt_id or data.get("assurance") != "mfa":
                raise DomainError("RECEIPT_INVALID")
            safe_payload = dict(data["payload"])
            self.breaker.require_write(session, user_id,
                                       protective=record.capability_id != "X05" and self._is_protective(session, record.capability_id, safe_payload))
            receipt: ReceiptRecord | None = None
            if data.get("assurance") == "mfa" and data.get("receipt_id"):
                receipt = session.get(ReceiptRecord, receipt_id)
                if receipt is None or receipt.plan_id != record_id or receipt.subject_id != user_id:
                    raise DomainError("RECEIPT_INVALID")
                if receipt.status != "pending" or receipt.expires_at <= utc_now_naive():
                    raise DomainError("RECEIPT_EXPIRED")
                if (receipt.action_hash != data.get("action_hash")
                        or receipt.display_hash != data.get("display_hash")):
                    raise DomainError("HASH_MISMATCH")
            # Direct-MFA records can use a challenge bound to the record rather
            # than a ReceiptRecord. Both paths must recheck the authorized
            # payload and display after OTP verification, inside the commit.
            if capability_id not in {"T01", "T02", "T03"}:
                if capability_id == "X05":
                    expected_action, expected_display = self._loss_report_hashes(
                        capability_id, record_id, str(safe_payload.get("card_id", "card_001"))
                    )
                else:
                    expected_action, expected_display = self._payload_hashes(capability_id, safe_payload)
                if capability_id != 'X05' and 'display' in data:
                    expected_display = self._payload_display_hash(expected_action, data.get('display', []))
                if (data.get("action_hash") != expected_action
                        or data.get("display_hash") != expected_display):
                    raise DomainError("HASH_MISMATCH")
            payload = safe_payload | {"_mfa_verified": True}
            result_data, status = self._dispatch(user_id, record.capability_id, payload, record.idempotency_key)
            record.status = status
            record.data_json = json.dumps(result_data, ensure_ascii=False, sort_keys=True)
            if receipt is not None:
                receipt.status = "consumed"
                session.add(ReceiptConsumption(receipt_id=receipt_id, node_id=record_id,
                                               operation_id=record_id, consumed_at=utc_now_naive()))
                self.audit.append(session, f"trace_{record_id}", user_id, "authorization.consumed",
                                  {"record_id": record_id, "receipt_id": receipt_id, "status": status})
            self.audit.append(session, f"trace_{record_id}", user_id, f"capability.{record.capability_id}.mfa_completed",
                              {"record_id": record_id, "status": status})
            return self._result(record)

    def confirm_capability(self, user_id: str, record_id: str, requested_receipt_id: str,
                           action_hash_value: str, display_hash_value: str) -> CapabilityResult:
        otp_receipt_id: str | None = None
        mfa_receipt_id: str | None = None
        with self.database.atomic():
            session = self.database.sessions()
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if record.status != "awaiting_confirmation":
                raise DomainError("RECEIPT_REPLAYED")
            data = json.loads(record.data_json)
            receipt_id = str(data.get("receipt_id", ""))
            if requested_receipt_id != receipt_id:
                raise DomainError("RECEIPT_INVALID")
            receipt = session.get(ReceiptRecord, receipt_id)
            if receipt is None or receipt.subject_id != user_id or receipt.plan_id != record_id:
                raise DomainError("RECEIPT_INVALID")
            if receipt.status != "pending":
                raise DomainError("RECEIPT_REPLAYED")
            if receipt.expires_at <= utc_now_naive():
                raise DomainError("RECEIPT_EXPIRED")
            if receipt.plan_revision != 1:
                raise DomainError("PLAN_REVISION_MISMATCH")
            if data.get("action_hash") != action_hash_value or data.get("display_hash") != display_hash_value:
                raise DomainError("HASH_MISMATCH")
            if receipt.action_hash != action_hash_value or receipt.display_hash != display_hash_value:
                raise DomainError("HASH_MISMATCH")
            assurance = data.get("assurance", "confirm")
            if self._requires_mfa(session, user_id, record.capability_id, data.get('payload', {})) and assurance != 'mfa':
                raise DomainError('MFA_REQUIRED')
            self.breaker.require_write(session, user_id,
                                       protective=self._is_protective(session, record.capability_id, data.get("payload", {})))
            capability_id = record.capability_id
            idempotency_key = record.idempotency_key
            if capability_id not in {"T01", "T02", "T03"}:
                expected_action, expected_display = self._payload_hashes(
                    capability_id, dict(data.get("payload", {}))
                )
                if data.get("flow") == "merchant_price_reconfirmation" or 'display' in data:
                    expected_display = self._payload_display_hash(expected_action, data.get("display", []))
                if data.get("action_hash") != expected_action or data.get("display_hash") != expected_display:
                    raise DomainError("HASH_MISMATCH")
            if assurance != "mfa" or not self.require_device_mfa:
                claimed = cast(Any, session.execute(
                    update(CapabilityRecord)
                    .where(CapabilityRecord.id == record_id,
                           CapabilityRecord.user_id == user_id,
                           CapabilityRecord.status == "awaiting_confirmation")
                    .values(status="executing")
                ))
                if claimed.rowcount != 1:
                    raise DomainError("RECEIPT_REPLAYED")
                record.status = "executing"
                receipt.status = "consumed"
                session.add(ReceiptConsumption(receipt_id=receipt_id, node_id=record_id,
                                               operation_id=record_id, consumed_at=utc_now_naive()))
                self.audit.append(session, f"trace_{record_id}", user_id, "authorization.consumed",
                                  {"record_id": record_id, "receipt_id": receipt_id, "status": "executing"})
            if assurance == "mfa" and self.require_device_mfa:
                record.status = "awaiting_mfa"
                self.audit.append(session, f"trace_{record_id}", user_id,
                                  "authorization.confirmed.awaiting_mfa",
                                  {"record_id": record_id, "receipt_id": receipt_id})
                otp_receipt_id = receipt_id if user_id in self.device_tokens else None
                result = self._result(record)
            else:
                payload = dict(data.get("payload", {})) | {"_confirmed": True,
                    "_mfa_verified": assurance == 'mfa' and not self.require_device_mfa}
                if data.get("flow") == "merchant_price_reconfirmation":
                    result_data, status = self._commit_merchant_reconfirmation(
                        session, user_id, record, data
                    )
                else:
                    result_data, status = self._dispatch(user_id, capability_id, payload, idempotency_key)
                if assurance == "mfa" and self.require_device_mfa:
                    status = "awaiting_mfa"
                if capability_id == "X05":
                    card_id = str(result_data.get("card_id", payload.get("card_id", "card_001")))
                    action_digest, display_digest = self._loss_report_hashes(capability_id, record_id, card_id)
                    mfa_receipt_id = f"rcp_{uuid.uuid4().hex}"
                    status = "awaiting_mfa"
                    result_data.update({"receipt_id": mfa_receipt_id, "action_hash": action_digest,
                                        "display_hash": display_digest, "assurance": "mfa",
                                        "next_step": "verify_loss_report", "payload": dict(payload)})
                record = session.get(CapabilityRecord, record_id)
                if record is None or record.status != "executing":
                    raise DomainError("CAPABILITY_RECORD_NOT_FOUND")
                record.status = status
                if mfa_receipt_id is None:
                    result_data.update({"action_hash": action_hash_value, "display_hash": display_hash_value,
                                        "receipt_id": receipt_id})
                    if data.get("flow") == "merchant_price_reconfirmation":
                        result_data["display"] = data.get("display", [])
                record.data_json = json.dumps(result_data, ensure_ascii=False, sort_keys=True)
                if mfa_receipt_id is not None:
                    session.add(ReceiptRecord(id=mfa_receipt_id, subject_id=user_id, plan_id=record_id,
                                              plan_revision=1, node_ids_json=json.dumps([record_id]),
                                              action_hash=action_digest, display_hash=display_digest,
                                              assurance="mfa", issued_at=utc_now_naive(),
                                              expires_at=utc_now_naive() + timedelta(minutes=5), status="pending"))
                    otp_receipt_id = mfa_receipt_id if user_id in self.device_tokens else None
                self.audit.append(session, f"trace_{record_id}", user_id,
                                  f"capability.{capability_id}.confirmed",
                                  {"record_id": record_id, "status": status})
                result = self._result(record)
        if mfa_receipt_id is not None and user_id in self.device_tokens:
            DeviceService(self.database).issue_otp(user_id, mfa_receipt_id, action_digest)
        elif otp_receipt_id is not None:
            DeviceService(self.database).issue_otp(user_id, otp_receipt_id, str(data.get("action_hash", "")))
        return result

    def get(self, user_id: str, record_id: str) -> CapabilityResult:
        with self.database.sessions() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
            return self._result(record)

    def invalidate_prepared(self, user_id: str, record_id: str, *, cancelled: bool = False) -> CapabilityResult:
        with self.database.sessions.begin() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if record.status not in {"awaiting_confirmation", "awaiting_mfa"}:
                raise DomainError("CAPABILITY_STATE_NOT_EDITABLE")
            data = json.loads(record.data_json)
            reconfirmation = data.get("reconfirmation")
            if data.get("flow") == "merchant_price_reconfirmation" and isinstance(reconfirmation, dict):
                old_order = session.get(MerchantOrder, str(reconfirmation.get("order_id", "")))
                if old_order is not None and old_order.user_id == user_id and old_order.status == "scheduled":
                    old_order.status = "cancelled"
                old_mandate = session.get(Mandate, str(reconfirmation.get("old_mandate_id", "")))
                if old_mandate is not None and old_mandate.user_id == user_id and old_mandate.used_count == 0:
                    old_mandate.status = "revoked"
                old_quote = session.get(MerchantQuote, str(reconfirmation.get("old_quote_id", "")))
                if old_quote is not None and old_quote.user_id == user_id:
                    old_quote.status = "invalidated"
                payload = data.get("payload", {})
                new_quote = session.get(MerchantQuote, str(payload.get("quote_id", "")))
                if new_quote is not None and new_quote.user_id == user_id and new_quote.status == "active":
                    new_quote.status = "invalidated"
                reservation_id = str(payload.get("reservation_id") or
                                      (old_order.reservation_id if old_order is not None else "") or "")
                reservation = session.get(Reservation, reservation_id) if reservation_id else None
                if reservation is not None and reservation.user_id == user_id and reservation.status == "active":
                    account = session.get(Account, reservation.account_id)
                    if account is None:
                        raise DomainError("ACCOUNT_NOT_FOUND")
                    account.available_minor += reservation.amount_minor
                    reservation.status = "released"
            receipt_ids = {str(data.get("receipt_id", ""))}
            plan_id = str(data.get("plan_id", ""))
            if plan_id:
                receipts = session.scalars(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan_id)).all()
                receipt_ids.update(item.id for item in receipts)
                plan = session.get(__import__("agentbank.db", fromlist=["PlanRecord"]).PlanRecord, plan_id)
                if plan is not None:
                    plan.status = "cancelled" if cancelled else "invalidated"
            for receipt_id in receipt_ids - {""}:
                receipt = session.get(ReceiptRecord, receipt_id)
                if receipt is not None and receipt.status == "pending":
                    receipt.status = "invalidated"
            record.status = "cancelled" if cancelled else "invalidated"
            data["status"] = record.status
            record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
            self.audit.append(session, f"trace_{record.id}", user_id,
                              "capability.cancelled" if cancelled else "capability.reprepared",
                              {"record_id": record.id, "capability_id": record.capability_id,
                               "invalidated_receipts": sorted(receipt_ids - {""})})
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

    def reject_card_application(self, user_id: str, record_id: str, reason: str) -> CapabilityResult:
        with self.database.sessions.begin() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id or record.capability_id != "C01":
                raise DomainError("NOT_OWNER")
            data = json.loads(record.data_json)
            if data.get("status") not in {"pending", "reviewing", "production"}:
                raise DomainError("CARD_APPLICATION_STATE_INVALID")
            card_id = str(data.get("card_id", ""))
            card = session.get(Card, card_id)
            application = session.scalar(select(CardApplication).where(CardApplication.card_id == card_id))
            if card is not None:
                card.status = "rejected"
            if application is not None:
                application.status = "rejected"
            data.update({"status": "rejected", "application_stage": "rejected", "rejection_reason": reason})
            record.status = "failed"
            record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
            self.audit.append(session, f"trace_{record.id}", user_id, "card.application.rejected",
                              {"record_id": record.id, "reason": reason})
            return self._result(record)

    def retry_card_application(self, user_id: str, record_id: str, idempotency_key: str) -> CapabilityResult:
        with self.database.sessions() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id or record.capability_id != "C01":
                raise DomainError("NOT_OWNER")
            data = json.loads(record.data_json)
            if data.get("status") != "rejected":
                raise DomainError("CARD_APPLICATION_STATE_INVALID")
        return self.execute(user_id, "C01", {"virtual": False, "assurance": "mfa"}, idempotency_key)

    def revoke_mandate(self, user_id: str, mandate_id: str) -> None:
        with self.database.sessions.begin() as session:
            mandate = session.get(Mandate, mandate_id)
            if mandate is None or mandate.user_id != user_id:
                raise DomainError("NOT_OWNER")
            mandate.status = "revoked"
            self.audit.append(session, f"trace_{mandate_id}", user_id, "mandate.revoked", {"mandate_id": mandate_id})

    def simulate_subscription_charge(self, user_id: str, subscription_id: str) -> dict[str, Any]:
        with self.database.atomic() as session:
            subscription = session.get(Subscription, subscription_id)
            if subscription is None or subscription.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if subscription.status != "active":
                return {"subscription_id": subscription_id, "charged": False, "reason": "CANCELLED"}
            operation_id = f'op_subscription_{subscription_id}_{subscription.next_charge_at[:10]}'
            transaction_id = f'txn_{operation_id}_out'
            if session.get(Operation, operation_id) is not None:
                return {"subscription_id": subscription_id, "charged": True, "transaction_id": transaction_id,
                        "amount_minor": subscription.amount_minor}
            subscription_account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
            try:
                bucket = require_payment(self.database, session, user_id, subscription_account,
                                         subscription.amount_minor, self.clock.business_date())
            except DomainError as error:
                self.audit.append(session, f'trace_{operation_id}', user_id, 'subscription.charge.blocked', {'reason': error.code})
                return {"subscription_id": subscription_id, "charged": False, "reason": error.code}
            assert subscription_account is not None
            session.add(Operation(id=operation_id, user_id=user_id, idempotency_key=operation_id,
                                  plan_id=subscription_id, node_id=subscription_id, status='succeeded',
                                  amount_minor=subscription.amount_minor, recipient_id=f'subscription:{subscription_id}'))
            post_payment(session, operation_id, subscription_account, f'subscription:{subscription_id}',
                         subscription.amount_minor, self.clock.business_date(), category='订阅',
                         merchant=subscription.merchant, direction='debit')
            bucket.completed_minor += subscription.amount_minor
            self.audit.append(session, f"trace_{transaction_id}", user_id, "subscription.charge.simulated",
                              {"subscription_id": subscription_id, "transaction_id": transaction_id,
                               "amount_minor": subscription.amount_minor})
            return {"subscription_id": subscription_id, "charged": True, "transaction_id": transaction_id,
                    "amount_minor": subscription.amount_minor}

    def dispatch_reminders(self, user_id: str | None = None) -> list[dict[str, str]]:
        today = date.fromisoformat(self.clock.business_date())
        sent: list[dict[str, str]] = []
        with self.database.sessions.begin() as session:
            query = select(Reminder, Subscription).join(Subscription, Subscription.id == Reminder.subscription_id).where(
                Reminder.status == "scheduled")
            if user_id:
                query = query.where(Reminder.user_id == user_id)
            for reminder, subscription in session.execute(query).all():
                charge_date = date.fromisoformat(subscription.next_charge_at[:10])
                due_date = charge_date - timedelta(days=reminder.remind_before_days)
                if today < due_date:
                    continue
                dedupe_key = f"reminder:{reminder.id}"
                existing = session.scalar(select(OutboxEvent).where(OutboxEvent.dedupe_key == dedupe_key))
                if existing is None:
                    session.add(OutboxEvent(id=f"out_{uuid.uuid4().hex}", user_id=reminder.user_id,
                                            event_type="subscription.reminder", dedupe_key=dedupe_key,
                                            payload_json=json.dumps({"reminder_id": reminder.id,
                                                                     "subscription_id": reminder.subscription_id,
                                                                     "merchant": subscription.merchant,
                                                                     "charge_date": subscription.next_charge_at},
                                                                    ensure_ascii=False, sort_keys=True),
                                            status="pending"))
                reminder.status = "sent"
                sent.append({"reminder_id": reminder.id, "dedupe_key": dedupe_key, "status": "sent"})
        return sent

    def deliver_reminders(self, user_id: str | None = None) -> list[dict[str, str]]:
        delivered: list[dict[str, str]] = []
        with self.database.sessions.begin() as session:
            query = select(OutboxEvent).where(OutboxEvent.event_type == "subscription.reminder",
                                              OutboxEvent.status == "pending")
            if user_id:
                query = query.where(OutboxEvent.user_id == user_id)
            for event in session.scalars(query).all():
                event.status = "delivered"
                payload = json.loads(event.payload_json)
                if session.scalar(select(UserNotification).where(UserNotification.dedupe_key == event.dedupe_key)) is None:
                    session.add(UserNotification(id=f"notice_{uuid.uuid4().hex}", user_id=event.user_id,
                                                 event_type=event.event_type,
                                                 title="订阅续费提醒",
                                                 body=f"{payload.get('merchant', '订阅')}将在{payload.get('charge_date', '')}续费。",
                                                 dedupe_key=event.dedupe_key, status="unread"))
                delivered.append({"reminder_id": json.loads(event.payload_json)["reminder_id"],
                                  "dedupe_key": event.dedupe_key, "status": "sent"})
        return delivered

    def list_notifications(self, user_id: str, unread_only: bool = False) -> list[dict[str, Any]]:
        with self.database.sessions() as session:
            query = select(UserNotification).where(UserNotification.user_id == user_id).order_by(UserNotification.created_at.desc())
            if unread_only:
                query = query.where(UserNotification.status == "unread")
            return [{"notification_id": item.id, "event_type": item.event_type, "title": item.title,
                     "body": item.body, "status": item.status, "created_at": item.created_at.isoformat()}
                    for item in session.scalars(query).all()]

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
        from agentbank.capability_contracts import validate_result

        data = json.loads(record.data_json)
        try:
            validated = validate_result(record.capability_id, record.status, data)
        except Exception as error:
            raise DomainError("CAPABILITY_RESULT_CONTRACT_INVALID") from error
        return CapabilityResult(capability_id=record.capability_id, record_id=record.id, status=record.status,
                                data=validated,
                                evidence={"audit_event": f"capability.{record.capability_id}.completed",
                                          "idempotency_key": record.idempotency_key})

    @staticmethod
    def _safe_payload(capability_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if capability_id != "C08":
            return dict(payload)
        safe = dict(payload)
        if "new_pin" in safe:
            safe["new_pin"] = "[REDACTED]"
        return safe

    def _payload_hashes(self, capability_id: str, payload: dict[str, Any]) -> tuple[str, str]:
        canonical = json.dumps({"capability_id": capability_id,
                                "payload": self._safe_payload(capability_id, payload)},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if capability_id == 'C08':
            return digest, self._payload_display_hash(digest, self._pin_display(payload))
        return digest, self._payload_display_hash(digest, self._capability_display(capability_id, payload))

    @staticmethod
    def _capability_display(capability_id: str, payload: dict[str, Any]) -> list[dict[str, str]]:
        names = {'T04': '定时转账', 'T05': 'AA 收款', 'C01': '申请银行卡', 'C02': '调整卡片额度',
                 'C03': '新增交易限制', 'C04': '解除交易限制', 'C05': '挂失银行卡', 'C06': '解除挂失',
                 'C07': '冻结或解冻银行卡', 'W03': '提交风险测评', 'W04': '申购理财', 'W05': '赎回理财',
                 'S01': '查询订阅', 'S02': '设置续费提醒', 'S03': '取消订阅', 'X02': '预留资金',
                 'X03': '预订鲜花', 'X04': '预订蛋糕'}
        labels = {'recipient_name': '收款人', 'card_id': '卡片', 'amount_minor': '金额', 'total_minor': '总金额',
                  'recipient_user_id': '收款人', 'recipient_account_id': '收款账户',
                  'limit_minor': '新额度', 'next_due': '首次执行日期', 'period': '执行周期', 'max_uses': '执行次数',
                  'product_id': '理财产品', 'order_id': '赎回订单', 'subscription_id': '订阅', 'days': '提前提醒天数',
                  'delivery_date': '送达日期', 'dispatch_on': '下单日期', 'restriction': '限制类型', 'action': '操作'}
        values = {'monthly': '每月', 'weekly': '每周', 'daily': '每天', 'yearly': '每年', 'once': '单次',
                  'online': '网购', 'freeze': '冻结', 'unfreeze': '解冻'}
        rows = [{'label': '业务', 'value': names.get(capability_id, '银行业务')}]
        if capability_id == 'W03' and 'answers' in payload:
            rows.append({'label': '问卷版本', 'value': str(payload.get('questionnaire_version', VERSION))})
            rows.extend(answer_display(payload['answers']))
        for field, label in labels.items():
            if field in payload:
                value = f"¥{int(payload[field]) // 100}.{int(payload[field]) % 100:02d}" if field.endswith('_minor') else values.get(str(payload[field]), str(payload[field]))
                if field == 'recipient_account_id':
                    value = f'尾号 {str(payload[field])[-4:]}'
                rows.append({'label': label, 'value': value})
        rows.extend([{'label': '费用', 'value': '本地模拟免费'},
                     {'label': '生效条件', 'value': '确认且所需强验证通过后执行'},
                     {'label': '撤销', 'value': '提交前可取消；执行后以业务实际状态为准'}])
        return rows

    @staticmethod
    def _pin_display(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [{'label': '业务', 'value': '修改卡片 PIN（模拟）'},
                {'label': '卡片', 'value': str(payload.get('card_id', 'card_001'))},
                {'label': '生效时间', 'value': '确认且设备强验证通过后'},
                {'label': '费用', 'value': '本地模拟不收取费用'},
                {'label': '后果', 'value': '记录模拟变更结果，不保存新 PIN，不修改真实银行卡'},
                {'label': '撤销', 'value': '验证前可以取消；验证完成后须重新发起修改'}]

    @staticmethod
    def _payload_display_hash(action_hash_value: str, display: list[dict[str, Any]]) -> str:
        canonical = json.dumps({"action_hash": action_hash_value, "display": display},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _commit_merchant_reconfirmation(self, session: Any, user_id: str,
                                        record: CapabilityRecord,
                                        data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        payload = dict(data["payload"])
        details = dict(data["reconfirmation"])
        order = session.get(MerchantOrder, str(details.get("order_id", "")))
        quote = session.get(MerchantQuote, str(payload.get("quote_id", "")))
        catalog = session.get(MerchantCatalog, order.item if order else "")
        if order is None or order.user_id != user_id or order.status != "scheduled":
            raise DomainError("ORDER_NOT_RECONFIRMABLE")
        if quote is None or quote.user_id != user_id or quote.status != "active":
            raise DomainError("QUOTE_INVALID")
        if catalog is None or catalog.stock <= 0:
            raise DomainError("MERCHANT_OUT_OF_STOCK")
        if quote.price_version != catalog.price_version or quote.amount_minor != int(payload["amount_minor"]):
            raise DomainError("QUOTE_AMOUNT_MISMATCH")
        old_mandate = session.get(Mandate, str(details.get("old_mandate_id", "")))
        if old_mandate is not None and old_mandate.user_id == user_id and old_mandate.used_count == 0:
            old_mandate.status = "revoked"
        mandate_id = f"mdt_{uuid.uuid4().hex}"
        session.add(Mandate(id=mandate_id, user_id=user_id, plan_id=record.id,
                            operation_type="merchant.order.create", subject_ref=order.item,
                            max_amount_minor=int(payload["amount_minor"]), max_uses=1,
                            starts_on=self.clock.business_date(), expires_on=order.delivery_date,
                            status="active"))
        order.amount_minor = int(payload["amount_minor"])
        order.quote_id = quote.id
        quote.status = "accepted"
        return {"quote_id": quote.id, "order_id": order.id, "item": order.item,
                "status": "scheduled", "delivery_date": order.delivery_date,
                "dispatch_on": payload.get("dispatch_on", self.clock.business_date()),
                "amount_minor": order.amount_minor, "mandate_id": mandate_id}, "scheduled"

    @staticmethod
    def _loss_report_hashes(capability_id: str, record_id: str, card_id: str) -> tuple[str, str]:
        canonical = json.dumps({"capability_id": capability_id, "record_id": record_id,
                                "step": "loss_report", "card_id": card_id},
                               sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return digest, digest

    @staticmethod
    def _is_protective(session: Any, capability_id: str, payload: dict[str, Any]) -> bool:
        if capability_id == "C03" or capability_id == "X05":
            return True
        if capability_id == "C07":
            return bool(payload.get("action", "freeze") != "unfreeze")
        if capability_id == "C02":
            card = session.get(Card, str(payload.get("card_id", "card_001")))
            return bool(card is not None and int(payload.get("limit_minor", card.limit_minor)) <= card.limit_minor)
        return False

    def _dispatch(self, user_id: str, capability_id: str, payload: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], str]:
        from agentbank.security.tool_breaker import guarded_call
        read_only = capability_id in {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "X01"}
        read_only = read_only or (capability_id == "T05" and payload.get("action") in {"preview", "status"})
        def commit() -> tuple[dict[str, Any], str]:
            with self.database.atomic() as session:
                if not read_only:
                    self.breaker.require_write(session, user_id,
                        protective=self._is_protective(session, capability_id, payload))
                if capability_id == 'W04':
                    require_safe_risk(session, user_id)
                if self._requires_mfa(session, user_id, capability_id, payload):
                    verified = payload.get('_mfa_verified') or (not self.require_device_mfa and payload.get('assurance') == 'mfa')
                    if not verified:
                        raise DomainError('MFA_REQUIRED')
                return self._dispatch_handler(user_id, capability_id, payload, idempotency_key)
        return guarded_call(self.database, capability_id,
                            commit,
                            read_only=read_only)

    @staticmethod
    def _aa_snapshot(session: Any, user_id: str, bill_id: str) -> dict[str, Any]:
        bill = session.get(AABill, bill_id)
        if bill is None:
            raise DomainError('BILL_NOT_FOUND')
        share = session.scalar(select(AAShare).where(AAShare.bill_id == bill_id, AAShare.participant_id == user_id))
        if share is None:
            raise DomainError('SHARE_NOT_FOUND')
        recipient = session.scalar(select(Account).where(Account.user_id == bill.user_id).order_by(Account.id))
        source = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
        if recipient is None or source is None:
            raise DomainError('ACCOUNT_NOT_FOUND')
        return {'amount_minor': share.amount_minor, 'recipient_user_id': bill.user_id,
                'recipient_account_id': recipient.id, 'source_account_id': source.id}

    def _requires_mfa(self, session: Any, user_id: str, capability_id: str, payload: dict[str, Any]) -> bool:
        if capability_id == 'C02':
            card = session.get(Card, str(payload.get('card_id', 'card_001')))
            if card is None or card.user_id != user_id:
                raise DomainError('NOT_OWNER')
            self.breaker.require_write(session, user_id, protective=self._is_protective(session, capability_id, payload))
            return bool(int(payload.get('limit_minor', card.limit_minor)) > card.limit_minor)
        if capability_id == 'T05' and payload.get('action') == 'pay_share':
            snapshot = self._aa_snapshot(session, user_id, str(payload['bill_id']))
            if any(payload.get(key) != value for key, value in snapshot.items()):
                raise DomainError('PAYMENT_SNAPSHOT_CHANGED')
            self.breaker.require_write(session, user_id)
            risk = session.get(RiskContextRecord, user_id)
            if risk is not None and risk.expires_at > utc_now_naive():
                if risk.level == 'high':
                    raise DomainError('FRAUD_REVIEW_REQUIRED')
                if risk.level in {'medium', 'unknown'}:
                    raise DomainError('RISK_REVIEW_REQUIRED')
            bucket = TransferService._bucket(session, user_id, self.clock.business_date())
            decision = decide('transfer.create', PolicyContext(projected_today_minor=
                bucket.completed_minor + bucket.pending_minor + int(snapshot['amount_minor'])))
            return decision.assurance is Assurance.mfa
        return False

    def _dispatch_handler(self, user_id: str, capability_id: str, payload: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], str]:
        if capability_id == "Q01":
            account = self._account(user_id)
            return {"account_id": account.id, "ledger_balance_minor": account.ledger_balance_minor,
                    "available_minor": account.available_minor, "currency": "CNY",
                    "as_of": datetime.now(timezone.utc).isoformat()}, "succeeded"
        if capability_id == "Q02":
            with self.database.sessions() as session:
                query = select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.posted_at)
                if payload.get("category"):
                    query = query.where(Transaction.category == str(payload["category"]))
                if payload.get("start_date"):
                    query = query.where(Transaction.posted_at >= str(payload["start_date"]))
                if payload.get("end_date"):
                    end_value = str(payload["end_date"])
                    if len(end_value) == 10:
                        next_date = (date.fromisoformat(end_value) + timedelta(days=1)).isoformat()
                        query = query.where(Transaction.posted_at < next_date)
                    else:
                        query = query.where(Transaction.posted_at <= end_value)
                rows = list(session.scalars(query.limit(int(payload.get("limit", 100)))).all())
                return {"transactions": [{"txn_id": row.id, "user_id": row.user_id, "posted_at": row.posted_at, "amount_minor": row.amount_minor,
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
            if payload.get("recipient_id"):
                prepared = TransferService(self.database).prepare_for_payee(
                    user_id, str(payload["recipient_id"]), int(payload.get("amount_minor", 0)), payload.get("memo"))
            else:
                prepared = TransferService(self.database).prepare(user_id, str(payload.get("recipient_name", "")),
                                                                  int(payload.get("amount_minor", 0)), payload.get("memo"))
            return {"plan_id": prepared.plan_id, "receipt_id": prepared.receipt_id, "display_hash": prepared.display_hash,
                    "action_hash": prepared.action_hash, "tier_display": prepared.tier_display,
                    "assurance": prepared.assurance.value, "display": prepared.display}, "prepared"
        if capability_id == "T04":
            period = str(payload.get("period", "monthly"))
            recipient_id_value = payload.get("recipient_id")
            if recipient_id_value:
                recipient_id = str(recipient_id_value)
            else:
                recipient_name = str(payload.get("recipient_name") or payload.get("alias") or "")
                if recipient_name:
                    recipient_id = RecipientResolver(self.database).by_name(user_id, recipient_name).id
                else:
                    recipient_id = "payee_landlord"
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
            if payload.get("action") == "preview":
                participants = [str(item) for item in payload.get("participant_ids", [user_id, "user_002", "user_003"])]
                total_minor = int(payload.get("total_minor", 10_000))
                return {"shares": split_bill(total_minor, participants, user_id), "preview_only": True}, "succeeded"
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
                    operation_id = f"aaop_{bill_id}_{user_id}"
                    existing_operation = session.get(Operation, operation_id)
                    if existing_operation is not None:
                        return {"bill_id": bill_id, "share_status": "paid", "bill_status": bill.status,
                                "amount_minor": share.amount_minor, "operation_id": operation_id}, "succeeded"
                    recipient_account = session.get(Account, str(payload['recipient_account_id']))
                    if recipient_account is None or recipient_account.status != 'active' or aa_account.status != 'active':
                        raise DomainError('ACCOUNT_NOT_ACTIVE')
                    TransferService._bucket(session, user_id, self.clock.business_date()).completed_minor += share.amount_minor
                    share.status = "paid"
                    session.add(Operation(id=operation_id, user_id=user_id, idempotency_key=f"aa-pay-{bill_id}-{user_id}",
                                          plan_id=bill_id, node_id=share.id, status="succeeded",
                                          amount_minor=share.amount_minor, recipient_id=recipient_account.id))
                    post_payment(session, operation_id, aa_account, recipient_account.id,
                                 share.amount_minor, self.clock.business_date(), category='AA转账', merchant='AA收款')
                    unpaid = session.scalar(select(AAShare).where(AAShare.bill_id == bill_id, AAShare.status != "paid"))
                    if unpaid is None:
                        bill.status = "settled"
                    return {"bill_id": bill_id, "share_status": share.status, "bill_status": bill.status,
                            "amount_minor": share.amount_minor, "operation_id": operation_id}, "succeeded"
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
            dsl = payload.get("dsl")
            if dsl is None:
                filters: list[dict[str, Any]] = [{"field": "direction", "op": "eq", "value": "debit"}]
                if payload.get("start_month"):
                    filters.append({"field": "posted_at", "op": "gte", "value": f"{payload['start_month']}-01"})
                if payload.get("end_month"):
                    from calendar import monthrange
                    year, month = map(int, str(payload["end_month"]).split("-"))
                    filters.append({"field": "posted_at", "op": "lte",
                                    "value": f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}T23:59:59.999999"})
                dsl = {"source": "transactions", "filters": filters, "group_by": ["category"], "limit": 100}
            else:
                # Consumption statistics must retain their business scope even
                # when the model supplies a syntactically valid DSL query.
                dsl = {**dsl, "filters": [*dsl.get("filters", []),
                                          {"field": "direction", "op": "eq", "value": "debit"}]}
            result = DslExecutor(self.database).run(user_id, dsl)
            return {"rows": result.rows, "query_hash": result.query_hash, "row_count": result.row_count}, "succeeded"
        if capability_id == "B02":
            return {"anomalies": detect_anomalies(self.database, user_id)}, "succeeded"
        if capability_id == "B03":
            return {"months": monthly_report(self.database, user_id, str(payload.get("start_month", "2026-01")),
                                              str(payload.get("end_month", "2026-12")))}, "succeeded"
        if capability_id == "B04":
            year_value = str(payload.get("year", str(payload.get("start_month", "2026-01"))[:4]))
            start_month = str(payload.get("start_month", f"{year_value}-01"))
            end_month = str(payload.get("end_month", f"{year_value}-12"))
            start_year = int(start_month[:4])
            end_year = int(end_month[:4])
            months = monthly_report(self.database, user_id, start_month, end_month)
            years = yearly_report(self.database, user_id, start_year, end_year)
            return {"year": year_value, "months": months, "years": years,
                    "missing_years": [row["year"] for row in years if row["missing"]]}, "succeeded"
        if capability_id == "W01":
            if not self._has_risk(user_id):
                raise DomainError("RISK_PROFILE_REQUIRED")
            products = RiskService(self.database).recommend(user_id)
            return {"products": [self._product_data(product) for product in products]}, "succeeded"
        if capability_id == "W02":
            with self.database.sessions() as session:
                product_query = select(Product)
                product_ids = payload.get("product_ids")
                if product_ids:
                    product_query = product_query.where(Product.id.in_([str(item) for item in product_ids]))
                products = list(session.scalars(product_query).all())
                return {"products": [self._product_data(product) for product in products]}, "succeeded"
        if capability_id == "W03":
            answers = payload.get('answers', [])
            profile = RiskService(self.database, self.clock).submit(user_id, answers, payload.get('questionnaire_version', VERSION))
            return {"level": profile.level, "score": profile.score, "expires_on": profile.expires_on,
                    'questionnaire_version': VERSION, 'answers': answers}, "succeeded"
        if capability_id == "W04":
            product_id = str(payload.get("product_id", "product_R1"))
            with self.database.sessions() as session:
                product = session.get(Product, product_id)
            if product is None:
                raise DomainError("PRODUCT_NOT_FOUND")
            if not self._suitable(user_id, product.risk_level):
                raise DomainError("SUITABILITY_MISMATCH")
            amount_minor = int(payload.get("amount_minor", 10_000))
            if amount_minor % 100:
                raise DomainError('INVESTMENT_AMOUNT_INCREMENT')
            with self.database.sessions.begin() as session:
                investment_account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
                investment_account = require_active(investment_account, user_id)
                if investment_account is None or investment_account.available_minor < amount_minor:
                    raise DomainError("INSUFFICIENT_FUNDS")
                order_id = f"order_{uuid.uuid4().hex}"
                position_id = f'pos_{order_id}'
                shares_count = amount_minor // 100
                session.add(InvestmentOrder(id=order_id, user_id=user_id, product_id=product_id, side="subscribe",
                                            amount_minor=amount_minor, shares=shares_count, status="subscribed", position_id=position_id))
                session.add(Position(id=position_id, user_id=user_id, product_id=product_id,
                                     shares=shares_count, cost_minor=amount_minor, market_value_minor=amount_minor,
                                     as_of=self.clock.now().replace(tzinfo=None)))
                operation_id = f"op_invest_sub_{order_id}"
                session.add(Operation(id=operation_id, user_id=user_id, idempotency_key=idempotency_key,
                                      plan_id=idempotency_key, node_id=order_id, status="succeeded",
                                      amount_minor=amount_minor, recipient_id=f"investment:{product_id}"))
                post_payment(session, operation_id, investment_account, f'investment:{product_id}',
                             amount_minor, self.clock.business_date(), category='理财', merchant=product.name)
            return {"order_id": order_id, "product_id": product_id, "status": "subscribed",
                    "shares": shares_count, "amount_minor": amount_minor, "funds_status": "debited",
                    "operation_id": operation_id}, "succeeded"
        if capability_id == "W05":
            order_id = str(payload.get("order_id", f"order_{uuid.uuid4().hex}"))
            with self.database.sessions.begin() as session:
                order = session.get(InvestmentOrder, order_id)
                if order is None or order.user_id != user_id:
                    raise DomainError("ORDER_NOT_REDEEMABLE")
                if order.side != "subscribe" or order.status != "subscribed":
                    raise DomainError("ORDER_NOT_REDEEMABLE")
                position = self._order_position(session, order)
                if position is None or position.shares < order.shares:
                    raise DomainError("INSUFFICIENT_POSITION")
                product = session.get(Product, order.product_id)
                if product is None:
                    raise DomainError('PRODUCT_NOT_FOUND')
                expected = self.clock.now() + timedelta(days=product.redemption_days)
                order.status = "processing"
                order.side = "redeem"
                order.expected_settlement_at = expected.replace(tzinfo=None)
            return {"order_id": order_id, "status": "processing", "expected_settlement_at": expected.isoformat()}, "processing"
        if capability_id == "W06":
            as_of = self.clock.now().replace(tzinfo=None)
            with self.database.sessions() as session:
                positions = session.scalars(select(Position).where(Position.user_id == user_id)).all()
                orders = session.scalars(select(InvestmentOrder).where(InvestmentOrder.user_id == user_id)).all()
                order_ids_by_product: dict[str, list[str]] = {}
                for order in orders:
                    order_ids_by_product.setdefault(order.product_id, []).append(order.id)
                valuation = [{"product_id": position.product_id, "shares": position.shares,
                              "cost_minor": position.cost_minor,
                              "market_value_minor": (max(0, position.cost_minor +
                                  position.cost_minor * max(0, (as_of.date() - position.as_of.date()).days) // 2000)
                                  if position.shares > 0 else 0),
                              "pnl_minor": ((max(0, position.cost_minor +
                                  position.cost_minor * max(0, (as_of.date() - position.as_of.date()).days) // 2000)
                                  - position.cost_minor) if position.shares > 0 else 0),
                              "order_ids": order_ids_by_product.get(position.product_id, [])} for position in positions]
            return {"positions": valuation, "source": "simulated_market", "as_of": as_of.isoformat()}, "succeeded"
        if capability_id.startswith("C"):
            return self._card(user_id, capability_id, payload, idempotency_key)
        if capability_id == "S01":
            return {"subscriptions": detect_subscriptions(self.database, user_id)}, "succeeded"
        if capability_id == "S02":
            subscription_id = str(payload.get("subscription_id", "sub_video"))
            with self.database.sessions.begin() as session:
                subscription = session.get(__import__("agentbank.db", fromlist=["Subscription"]).Subscription, subscription_id)
                if subscription is None or subscription.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                days = int(payload.get("days", 3))
                existing = session.scalar(select(Reminder).where(
                    Reminder.user_id == user_id,
                    Reminder.subscription_id == subscription_id,
                    Reminder.remind_before_days == days,
                    Reminder.status == "scheduled",
                ).order_by(Reminder.id))
                if existing is not None:
                    return {"reminder_id": existing.id, "subscription_id": subscription_id,
                            "remind_before_days": days, "dedupe_key": existing.idempotency_key}, "scheduled"
                reminder_id = f"rem_{uuid.uuid4().hex}"
                session.add(Reminder(id=reminder_id, user_id=user_id, subscription_id=subscription_id,
                                     remind_before_days=days, status="scheduled",
                                     idempotency_key=idempotency_key))
            return {"reminder_id": reminder_id, "subscription_id": subscription_id,
                    "remind_before_days": days, "dedupe_key": idempotency_key}, "scheduled"
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
            event_data = [{"event_id": event.id, "title": event.title, "event_date": event.event_date} for event in events]
            clues = [clue for event in event_data for clue in birthday_history_clues(self.database, user_id, event["event_date"])]
            return {"events": event_data, "history_clues": clues, "recommendation_only": True}, "succeeded"
        if capability_id == "X02":
            amount = int(payload.get("amount_minor", 100_000))
            planner = BirthdayPlanner(self.database, self.clock, MandateService(self.database, self.clock))
            reservation_id = planner.reserve(user_id, amount, str(payload.get("plan_id", idempotency_key)))
            return {"reservation_id": reservation_id, "amount_minor": amount, "ledger_balance_unchanged": True}, "reserved"
        if capability_id in {"X03", "X04"}:
            order_id = f"merchant_order_{uuid.uuid4().hex}"
            quote_id = str(payload["quote_id"])
            item = "flower" if capability_id == "X03" else "cake"
            delivery_date = str(payload.get("delivery_date", self.clock.business_date()))
            with self.database.sessions.begin() as session:
                quote = session.get(MerchantQuote, quote_id)
                catalog = session.get(MerchantCatalog, item)
                if (quote is None or quote.user_id != user_id or quote.item != item
                        or quote.delivery_date != delivery_date or quote.status != "active"
                        or quote.expires_at <= utc_now_naive()):
                    raise DomainError("QUOTE_INVALID")
                if catalog is None or catalog.stock <= 0:
                    raise DomainError("MERCHANT_OUT_OF_STOCK")
                if quote.price_version != catalog.price_version:
                    raise DomainError("PRICE_CHANGED")
                quote.status = "accepted"
            if payload.get("simulate_failure"):
                with self.database.sessions.begin() as session:
                    session.add(MerchantOrder(id=order_id, user_id=user_id, item=item, quote_id=quote_id,
                                              amount_minor=int(payload["amount_minor"]),
                                              delivery_date=str(payload.get("delivery_date", "2026-11-13")), status="failed",
                                              reservation_id=payload.get("reservation_id")))
                    reservation_id_value = payload.get("reservation_id")
                    compensation_reservation_id = str(reservation_id_value) if reservation_id_value else None
                    reservation = session.get(Reservation, compensation_reservation_id) if compensation_reservation_id else None
                    if compensation_reservation_id is not None and (reservation is None or reservation.user_id != user_id):
                        raise DomainError("NOT_OWNER")
                    compensation_account: Account | None = session.get(Account, reservation.account_id) if reservation is not None else None
                    if reservation is not None and reservation.status == "active":
                        if compensation_account is not None:
                            compensation_account.available_minor += reservation.amount_minor
                        reservation.status = "released"
                    operation_id = f"op_merchant_{order_id}"
                    session.add(Operation(id=operation_id, user_id=user_id,
                                          idempotency_key=f"merchant-{order_id}",
                                          plan_id=reservation.plan_id if reservation else idempotency_key,
                                          node_id=order_id, status="failed",
                                          amount_minor=int(payload["amount_minor"]),
                                          recipient_id=f"merchant:{item}", error_code="MERCHANT_UNAVAILABLE"))
                    self.audit.append(session, f"trace_{operation_id}", user_id,
                                      "merchant.order.compensated",
                                      {"order_id": order_id, "status": "failed",
                                       "reservation_id": reservation.id if reservation else None,
                                       "compensation_status": "reservation_released" if reservation else "not_required"})
                return {"quote_id": quote_id, "order_id": order_id, "item": item, "status": "failed",
                        "delivery_date": payload.get("delivery_date", self.clock.business_date()),
                        "reservation_id": payload.get("reservation_id"),
                        "compensation_status": "reservation_released" if payload.get("reservation_id") else "not_required",
                        "failure_code": "MERCHANT_UNAVAILABLE", "operation_id": operation_id}, "failed"
            delivery_date = str(payload.get("delivery_date", "2026-11-13"))
            dispatch_on = str(payload.get("dispatch_on", self.clock.business_date()))
            mandate_id = f"mdt_{uuid.uuid4().hex}"
            with self.database.sessions.begin() as session:
                session.add(MerchantOrder(id=order_id, user_id=user_id, item=item, quote_id=quote_id,
                                          amount_minor=int(payload.get("amount_minor", 5_000)),
                                          delivery_date=delivery_date, status="scheduled",
                                          reservation_id=payload.get("reservation_id")))
                session.add(Mandate(id=mandate_id, user_id=user_id, plan_id=idempotency_key,
                                    operation_type="merchant.order.create", subject_ref=item,
                                    max_amount_minor=int(payload["amount_minor"]), max_uses=1,
                                    starts_on=dispatch_on, expires_on=delivery_date, status="active"))
            return {"quote_id": quote_id, "order_id": order_id, "item": item, "status": "scheduled",
                    "delivery_date": delivery_date, "dispatch_on": dispatch_on, "amount_minor": int(payload["amount_minor"]),
                    "mandate_id": mandate_id, "reservation_id": payload.get("reservation_id")}, "scheduled"
        if capability_id == "X05":
            card_id = str(payload.get("card_id", "card_001"))
            if payload.get("_mfa_verified"):
                with self.database.sessions.begin() as session:
                    card = session.get(Card, card_id)
                    if card is None or card.user_id != user_id or card.status != "frozen":
                        raise DomainError("INVALID_CARD_STATE")
                    card.status = "lost"
                return {"card_id": card_id, "freeze_status": "lost", "loss_report_status": "succeeded",
                        "payment_blocked": True}, "succeeded"
            CardService(self.database).freeze(user_id, card_id)
            return {"card_id": card_id, "freeze_status": "frozen", "loss_report_status": "awaiting_mfa",
                    "next_step": "verify_loss_report"}, "partially_completed"
        raise DomainError("CAPABILITY_NOT_IMPLEMENTED")

    def _card(self, user_id: str, capability_id: str, payload: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], str]:
        if capability_id == "C07":
            if payload.get("action") == "unfreeze":
                card_id = str(payload.get("card_id", "card_001"))
                with self.database.sessions.begin() as session:
                    card = session.get(Card, card_id)
                    if card is None or card.user_id != user_id:
                        raise DomainError("NOT_OWNER")
                    if card.status != "frozen":
                        raise DomainError("INVALID_CARD_STATE")
                    card.status = "active"
                return {"card_id": card_id, "status": "active", "payment_blocked": False}, "succeeded"
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
            application_stage = "delivered" if payload.get("virtual", True) else "application"
            with self.database.sessions.begin() as session:
                session.add(Card(id=card_id, user_id=user_id, status=card_status))
                session.add(CardApplication(id=f"cardapp_{uuid.uuid4().hex}", card_id=card_id, user_id=user_id,
                                            card_type="virtual" if payload.get("virtual", True) else "physical",
                                            status=card_status))
            return {"card_id": card_id, "status": card_status, "application_stage": application_stage,
                    "card_type": "virtual" if payload.get("virtual", True) else "physical"}, "succeeded" if card_status == "active" else "pending"
        if capability_id == "C02":
            card_id = str(payload.get("card_id", "card_001"))
            with self.database.sessions.begin() as session:
                card = session.get(Card, card_id)
                if card is None or card.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                limit_minor = int(payload.get("limit_minor", card.limit_minor))
                if limit_minor > 500_000:
                    raise DomainError("CARD_LIMIT_EXCEEDED")
                card.limit_minor = limit_minor
            return {"card_id": card_id, "limit_minor": limit_minor}, "succeeded"
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
            with self.database.sessions.begin() as session:
                card = session.get(Card, str(payload.get('card_id', 'card_001')))
                if card is None or card.user_id != user_id:
                    raise DomainError('NOT_OWNER')
                pending = session.get(PendingPinChange, idempotency_key)
                if (pending is None or pending.user_id != user_id or pending.card_id != card.id
                        or pending.status != 'pending' or pending.expires_at <= utc_now_naive()):
                    raise DomainError('PIN_CHANGE_EXPIRED')
                card.pin_hash = pending.pin_hash
                pending.status = 'consumed'
            return {"card_id": card.id, "status": "pin_changed", "secret_persisted": False}, "succeeded"
        raise DomainError("CAPABILITY_NOT_IMPLEMENTED")

    @staticmethod
    def _hash_pin(pin: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', pin.encode(), salt, 600_000)
        return f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"

    @staticmethod
    def _pin_matches(pin: str, stored: str) -> bool:
        _, rounds, salt, expected = stored.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', pin.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)

    def verify_card_pin(self, user_id: str, card_id: str, pin: str) -> bool:
        """Internal simulator verifier; deliberately not exposed as an online guessing API."""
        with self.database.sessions() as session:
            card = session.get(Card, card_id)
            if card is None or card.user_id != user_id:
                raise DomainError('NOT_OWNER')
            return card.pin_hash is not None and self._pin_matches(pin, card.pin_hash)

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

    @staticmethod
    def _order_position(session: Session, order: InvestmentOrder) -> Position | None:
        if order.position_id:
            position = session.get(Position, order.position_id)
            if position is None or position.user_id != order.user_id or position.product_id != order.product_id:
                raise DomainError("POSITION_LINK_INVALID")
            return position
        # Legacy orders can be linked only when the lot is unambiguous.
        candidates = session.scalars(select(Position).where(
            Position.user_id == order.user_id, Position.product_id == order.product_id,
            Position.shares == order.shares, Position.cost_minor == order.amount_minor)).all()
        linked = set(session.scalars(select(InvestmentOrder.position_id).where(
            InvestmentOrder.id != order.id, InvestmentOrder.position_id.is_not(None))).all())
        candidates = [position for position in candidates if position.id not in linked]
        if len(candidates) != 1:
            raise DomainError("POSITION_RECONCILIATION_REQUIRED")
        order.position_id = candidates[0].id
        return candidates[0]

    def progress_investments(self, user_id: str | None = None, *, due_only: bool = False) -> list[CapabilityResult]:
        changed: list[CapabilityRecord] = []
        with self.database.atomic() as session:
            query = select(InvestmentOrder).where(InvestmentOrder.status == "processing", InvestmentOrder.side == "redeem")
            if due_only:
                query = query.where(InvestmentOrder.expected_settlement_at <= self.clock.now().replace(tzinfo=None))
            if user_id:
                query = query.where(InvestmentOrder.user_id == user_id)
            for order in session.scalars(query).all():
                account = session.scalar(select(Account).where(Account.user_id == order.user_id).order_by(Account.id))
                if account is None or account.status != "active":
                    continue
                try:
                    position = self._order_position(session, order)
                    if position is None or position.shares < order.shares:
                        raise DomainError("INSUFFICIENT_POSITION")
                except DomainError as error:
                    for record in session.scalars(select(CapabilityRecord).where(
                            CapabilityRecord.user_id == order.user_id, CapabilityRecord.capability_id == "W05")).all():
                        data = json.loads(record.data_json)
                        if data.get('order_id') != order.id or data.get('error_code') == error.code:
                            continue
                        data.update(error_code=error.code, next_step='manual_position_reconciliation')
                        record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                        self.audit.append(session, f'trace_{order.id}', order.user_id,
                            'investment.settlement.reconciliation_required',
                            {'order_id': order.id, 'error_code': error.code})
                        changed.append(record)
                    continue
                operation_id = f"op_invest_redeem_{order.id}"
                if session.get(Operation, operation_id) is None:
                    session.add(Operation(id=operation_id, user_id=order.user_id,
                        idempotency_key=operation_id, plan_id=order.id, node_id=order.id,
                        status="succeeded", amount_minor=order.amount_minor, recipient_id=account.id))
                    post_redemption(session, operation_id, account, f"investment:{order.product_id}",
                                    order.amount_minor, self.clock.business_date())
                    position.shares -= order.shares
                    position.cost_minor = max(0, position.cost_minor - order.amount_minor)
                    position.market_value_minor = position.cost_minor
                order.status = "settled"
                for record in session.scalars(select(CapabilityRecord).where(
                        CapabilityRecord.user_id == order.user_id, CapabilityRecord.capability_id == "W05")).all():
                    data = json.loads(record.data_json)
                    if data.get("order_id") != order.id:
                        continue
                    data.update(status="settled", operation_id=operation_id)
                    record.status = "succeeded"
                    record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                    changed.append(record)
        return [self._result(record) for record in changed]

    def reconfirm_merchant_price(self, user_id: str, record_id: str, amount_minor: int) -> CapabilityResult:
        if amount_minor <= 0:
            raise DomainError("INVALID_AMOUNT")
        with self.database.atomic() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id or record.capability_id not in {"X03", "X04"}:
                raise DomainError("NOT_OWNER")
            data = json.loads(record.data_json)
            if record.status != "paused" or data.get("error_code") != "PRICE_CHANGED":
                raise DomainError("CAPABILITY_STATE_NOT_RECONFIRMABLE")
            order = session.get(MerchantOrder, str(data.get("order_id", "")))
            if order is None or order.user_id != user_id or order.status != "scheduled":
                raise DomainError("ORDER_NOT_RECONFIRMABLE")
            catalog = session.get(MerchantCatalog, order.item)
            if catalog is not None and catalog.stock <= 0:
                raise DomainError("MERCHANT_OUT_OF_STOCK")
            if catalog is None or catalog.price_minor != amount_minor:
                raise DomainError("QUOTE_AMOUNT_MISMATCH")
            reservation = session.get(Reservation, order.reservation_id) if order.reservation_id else None
            if reservation is not None and (reservation.user_id != user_id or reservation.status != "active"
                                            or reservation.amount_minor < amount_minor):
                raise DomainError("RESERVATION_INSUFFICIENT")
            if reservation is None:
                account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
                if account is None or account.available_minor < amount_minor:
                    raise DomainError("INSUFFICIENT_FUNDS")
            today = self.clock.business_date()
            if order.delivery_date < today:
                raise DomainError("DELIVERY_DATE_EXPIRED")
            quote_id = f"quote_{uuid.uuid4().hex}"
            session.add(MerchantQuote(id=quote_id, user_id=user_id, item=order.item,
                                      amount_minor=amount_minor, delivery_date=order.delivery_date,
                                      price_version=catalog.price_version,
                                      expires_at=utc_now_naive() + timedelta(minutes=5), status="active"))
            payload = {key: data[key] for key in ("delivery_date", "dispatch_on", "reservation_id")
                       if key in data}
            payload.update({"amount_minor": amount_minor, "quote_id": quote_id})
            action_hash, display_hash = self._payload_hashes(record.capability_id, payload)
            display = [{"label": "商品", "value": "鲜花" if order.item == "flower" else "蛋糕"},
                       {"label": "更新后报价", "value": f"¥{amount_minor / 100:.2f}"},
                       {"label": "配送日期", "value": order.delivery_date}]
            display_hash = "sha256:" + hashlib.sha256(json.dumps(
                {"action_hash": action_hash, "display": display}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            receipt_id = f"rcp_{uuid.uuid4().hex}"
            session.add(ReceiptRecord(id=receipt_id, subject_id=user_id, plan_id=record.id,
                                      plan_revision=1, node_ids_json=json.dumps([record.id]),
                                      action_hash=action_hash, display_hash=display_hash,
                                      assurance="confirm", issued_at=utc_now_naive(),
                                      expires_at=utc_now_naive() + timedelta(minutes=5), status="pending"))
            record.status = "awaiting_confirmation"
            record.request_hash = "sha256:" + hashlib.sha256(json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            record.data_json = json.dumps({"capability_id": record.capability_id, "flow": "merchant_price_reconfirmation",
                                           "reconfirmation": {"order_id": order.id,
                                                              "old_mandate_id": str(data.get("mandate_id", "")),
                                                              "old_quote_id": order.quote_id},
                                           "payload": payload,
                                           "receipt_id": receipt_id, "action_hash": action_hash,
                                           "display_hash": display_hash, "assurance": "confirm",
                                           "display": display}, ensure_ascii=False, sort_keys=True)
            self.audit.append(session, f"trace_{record.id}", user_id, "merchant.order.reconfirmation_prepared",
                              {"record_id": record.id, "order_id": order.id, "quote_id": quote_id,
                               "amount_minor": amount_minor, "price_version": catalog.price_version})
            return self._result(record)

    def quote_merchant(self, user_id: str, item: str, delivery_date: str,
                       *, amount_minor: int | None = None) -> dict[str, Any]:
        date.fromisoformat(delivery_date)
        with self.database.sessions.begin() as session:
            catalog = session.get(MerchantCatalog, item)
            if catalog is None or item not in {"flower", "cake"}:
                raise DomainError("MERCHANT_ITEM_NOT_FOUND")
            if catalog.stock <= 0:
                raise DomainError("MERCHANT_OUT_OF_STOCK")
            amount = catalog.price_minor if amount_minor is None else amount_minor
            if amount <= 0:
                raise DomainError("INVALID_AMOUNT")
            quote = MerchantQuote(id=f"quote_{uuid.uuid4().hex}", user_id=user_id, item=item,
                                  amount_minor=amount, delivery_date=delivery_date, price_version=catalog.price_version,
                                  expires_at=utc_now_naive() + timedelta(minutes=5), status="active")
            session.add(quote)
            self.audit.append(session, f"trace_{quote.id}", user_id, "merchant.quote.created",
                              {"quote_id": quote.id, "item": item, "amount_minor": amount,
                               "price_version": catalog.price_version, "delivery_date": delivery_date})
            return {"quote_id": quote.id, "item": item, "amount_minor": amount,
                    "delivery_date": delivery_date, "source": "simulated_merchant",
                    "price_version": catalog.price_version, "expires_at": quote.expires_at.isoformat()}

    def get_merchant_quote(self, user_id: str, quote_id: str) -> dict[str, Any]:
        with self.database.sessions() as session:
            quote = session.get(MerchantQuote, quote_id)
            if quote is None or quote.user_id != user_id:
                raise DomainError("NOT_OWNER")
            catalog = session.get(MerchantCatalog, quote.item)
            return {"quote_id": quote.id, "item": quote.item, "amount_minor": quote.amount_minor,
                    "delivery_date": quote.delivery_date, "price_version": quote.price_version,
                    "source": "simulated_merchant", "status": quote.status,
                    "catalog_stock": catalog.stock if catalog else 0,
                    "current_price_version": catalog.price_version if catalog else None,
                    "current_price_minor": catalog.price_minor if catalog else None,
                    "expires_at": quote.expires_at.isoformat()}

    def update_merchant_catalog(self, item: str, *, price_minor: int | None = None, stock: int | None = None) -> None:
        with self.database.sessions.begin() as session:
            catalog = session.get(MerchantCatalog, item)
            if catalog is None:
                raise DomainError("MERCHANT_ITEM_NOT_FOUND")
            if price_minor is not None:
                if price_minor <= 0:
                    raise DomainError("INVALID_AMOUNT")
                catalog.price_minor = price_minor
                catalog.price_version += 1
            if stock is not None:
                if stock < 0:
                    raise DomainError("MERCHANT_STOCK_INVALID")
                catalog.stock = stock

    def retry_merchant_order(self, user_id: str, record_id: str) -> CapabilityResult:
        with self.database.atomic() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id or record.capability_id not in {"X03", "X04"}:
                raise DomainError("NOT_OWNER")
            data = json.loads(record.data_json)
            if record.status != "paused" or data.get("error_code") != "MERCHANT_OUT_OF_STOCK":
                raise DomainError("ORDER_NOT_RETRYABLE")
            order = session.get(MerchantOrder, str(data.get("order_id", "")))
            catalog = session.get(MerchantCatalog, order.item) if order else None
            if catalog is None or catalog.stock <= 0:
                raise DomainError("MERCHANT_OUT_OF_STOCK")
            record.status = "scheduled"
            data["status"] = "scheduled"
            data.pop("error_code", None)
            data.pop("next_step", None)
            record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
            self.audit.append(session, f"trace_{record.id}", user_id, "merchant.order.retry_requested",
                              {"record_id": record.id, "order_id": data["order_id"]})
            return self._result(record)

    def dispatch_due(self, user_id: str | None = None) -> list[CapabilityResult]:
        with self.database.atomic():
            return self._dispatch_due(user_id)

    def _dispatch_due(self, user_id: str | None = None) -> list[CapabilityResult]:
        today = self.clock.business_date()
        changed: list[CapabilityRecord] = []
        with self.database.sessions() as session:
            query = select(CapabilityRecord).where(CapabilityRecord.status.in_(["scheduled", "partially_completed"]),
                                                    CapabilityRecord.capability_id.in_(["T04", "X03", "X04"]))
            if user_id:
                query = query.where(CapabilityRecord.user_id == user_id)
            records = list(session.scalars(query).all())
        for record in records:
            parent_session = self.database.sessions()
            parent_node = parent_session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.idempotency_key == record.idempotency_key))
            if parent_node is not None:
                parent = parent_session.get(PlanRecord, parent_node.plan_id)
                if parent is not None and parent.status not in {'waiting_trigger', 'partially_completed'}:
                    continue
            data = json.loads(record.data_json)
            due = str(data.get("next_due") or data.get("dispatch_on") or data.get("delivery_date") or "9999-12-31")
            if due > today:
                continue
            status = "succeeded"
            if record.capability_id == "T04":
                try:
                    with self.database.sessions().begin_nested():
                        mandate = self.database.sessions().get(Mandate, str(data.get("mandate_id", "")))
                        if (mandate is None or mandate.user_id != record.user_id
                                or mandate.plan_id != str(data["schedule_id"])
                                or mandate.subject_ref != str(data["recipient_id"])
                                or mandate.operation_type != "transfer.create"
                                or not MandateService(self.database, self.clock).consume(
                                    mandate.id, int(data["amount_minor"]))):
                            raise DomainError("MANDATE_INVALID")
                        transfer = TransferService(self.database).commit_scheduled(
                            record.user_id, str(data["recipient_id"]), int(data["amount_minor"]),
                            str(data["schedule_id"]), record.id,
                            f"{record.id}-period-{int(data.get('dispatched_count', 0)) + 1}",
                            scheduled_business_date=today)
                        data["operation_id"] = transfer.operation_id
                        job_session = self.database.sessions()
                        job = job_session.scalar(select(ScheduledJob).where(ScheduledJob.schedule_id == str(data["schedule_id"]),
                                                                            ScheduledJob.status == "scheduled",
                                                                            ScheduledJob.due_date <= today)
                                                 .order_by(ScheduledJob.period_index))
                        if job is not None:
                            job.operation_id = transfer.operation_id
                            job.status = "succeeded"
                        data["dispatched_count"] = int(data.get("dispatched_count", 0)) + 1
                        data["max_uses"] = int(data.get("max_uses", 1)) - 1
                        if data["max_uses"] > 0 and data.get('period', 'monthly') != 'once':
                            first_job = job_session.scalar(select(ScheduledJob).where(
                                ScheduledJob.schedule_id == str(data['schedule_id'])).order_by(ScheduledJob.period_index))
                            next_due = next_scheduled_date(due, str(data.get('period', 'monthly')),
                                                           first_job.due_date if first_job else due)
                            data["next_due"] = next_due
                            job_session.add(ScheduledJob(id=f"job_{uuid.uuid4().hex}", user_id=record.user_id,
                                                        schedule_id=str(data["schedule_id"]), due_date=next_due,
                                                        period_index=int(data["dispatched_count"]) + 1,
                                                        idempotency_key=f"{record.id}-period-{int(data['dispatched_count']) + 1}",
                                                        status="scheduled"))
                            status = "scheduled"
                except DomainError as error:
                    data["error_code"] = error.code
                    status = "paused"
            else:
                order_id = str(data.get("order_id", ""))
                operation_id = f"op_merchant_{order_id}"
                data["operation_id"] = operation_id
                data["status"] = "succeeded"
                with self.database.sessions.begin() as order_session:
                    merchant = order_session.get(MerchantOrder, order_id)
                    if merchant is not None:
                        reservation = order_session.get(Reservation, merchant.reservation_id) if merchant.reservation_id else None
                        account = order_session.get(Account, reservation.account_id) if reservation else order_session.scalar(
                            select(Account).where(Account.user_id == record.user_id).order_by(Account.id)
                        )
                        operation = order_session.get(Operation, operation_id)
                        if merchant.status == "partially_completed":
                            merchant.status = "failed"
                            data["status"] = "failed"
                        elif operation is None:
                            if account is None:
                                raise DomainError("ACCOUNT_NOT_FOUND")
                            quote = order_session.get(MerchantQuote, merchant.quote_id)
                            catalog = order_session.get(MerchantCatalog, merchant.item)
                            if catalog is None or catalog.stock <= 0:
                                status = "paused"
                                data["status"] = "paused"
                                data["error_code"] = "MERCHANT_OUT_OF_STOCK"
                                data["next_step"] = "retry_when_available"
                            elif quote is None or quote.user_id != record.user_id or quote.status not in {"active", "accepted"}:
                                merchant.status = "failed"
                                data["status"] = "failed"
                                data["error_code"] = "QUOTE_INVALID"
                            elif quote.price_version != catalog.price_version or quote.amount_minor != merchant.amount_minor:
                                status = "paused"
                                data["status"] = "paused"
                                data["error_code"] = "PRICE_CHANGED"
                                data["next_step"] = "reconfirm_price"
                            elif quote.status == "active" and quote.expires_at <= utc_now_naive():
                                status = "paused"
                                data["status"] = "paused"
                                data["error_code"] = "QUOTE_EXPIRED"
                                data["next_step"] = "reconfirm_price"
                            else:
                                data["quote_id"] = quote.id
                            mandate_id = str(data.get("mandate_id", ""))
                            mandate = order_session.get(Mandate, mandate_id) if mandate_id else None
                            today_date = date.fromisoformat(today)
                            payment_bucket = None
                            if status != "paused" and data.get("status") != "failed":
                                try:
                                    payment_bucket = require_payment(
                                        self.database, order_session, record.user_id, account,
                                        merchant.amount_minor, today,
                                        held=merchant.amount_minor if reservation is not None else 0)
                                except DomainError as error:
                                    status = "paused"
                                    data.update(status="paused", error_code=error.code, next_step="review_security_state")
                            if status == "paused" or data.get("status") == "failed":
                                pass
                            elif int(data.get("amount_minor", merchant.amount_minor)) != merchant.amount_minor:
                                status = "paused"
                                data["status"] = "paused"
                                data["error_code"] = "PRICE_CHANGED"
                                data["next_step"] = "reconfirm_price"
                            elif (mandate is None or mandate.user_id != record.user_id
                                    or mandate.status != "active"
                                    or mandate.operation_type != "merchant.order.create"
                                    or mandate.subject_ref != merchant.item
                                    or today_date < date.fromisoformat(mandate.starts_on)
                                    or today_date > date.fromisoformat(mandate.expires_on)
                                    or merchant.amount_minor > mandate.max_amount_minor
                                    or mandate.used_count >= mandate.max_uses):
                                merchant.status = "failed"
                                data["status"] = "failed"
                                data["error_code"] = "MANDATE_INVALID"
                            elif reservation is not None and (reservation.user_id != record.user_id or reservation.status != "active"
                                                            or reservation.amount_minor < merchant.amount_minor):
                                merchant.status = "failed"
                                data["status"] = "failed"
                                data["error_code"] = "RESERVATION_INSUFFICIENT"
                            elif reservation is None and account.available_minor < merchant.amount_minor:
                                merchant.status = "failed"
                                data["status"] = "failed"
                                data["error_code"] = "INSUFFICIENT_FUNDS"
                            else:
                                if quote is not None:
                                    quote.status = "consumed"
                                if catalog is not None:
                                    catalog.stock -= 1
                                mandate.used_count += 1
                                post_payment(order_session, operation_id, account,
                                    f"merchant_settlement:{merchant.item}", merchant.amount_minor, today,
                                    category="礼品", merchant="鲜花商户" if merchant.item == "flower" else "蛋糕商户",
                                    direction="debit", held=merchant.amount_minor if reservation else 0)
                                if reservation is not None:
                                    reservation.amount_minor -= merchant.amount_minor
                                if payment_bucket is not None:
                                    payment_bucket.completed_minor += merchant.amount_minor
                                merchant.status = "delivered"
                                order_session.add(Operation(
                                    id=operation_id, user_id=record.user_id,
                                    idempotency_key=f"merchant-{order_id}",
                                    plan_id=reservation.plan_id if reservation else order_id,
                                    node_id=record.id, status="succeeded", amount_minor=merchant.amount_minor,
                                    recipient_id=f"merchant:{merchant.item}",
                                ))
                                data["status"] = "delivered"
                        else:
                            merchant.status = "delivered"
                            data["status"] = "delivered"
                        if reservation is not None:
                            order_session.flush()
                            pending_orders = order_session.scalars(select(MerchantOrder).where(
                                MerchantOrder.reservation_id == reservation.id,
                                MerchantOrder.status.notin_(["delivered", "failed", "cancelled"]),
                            )).all()
                            if not pending_orders and reservation.status == "active":
                                if reservation.amount_minor > 0 and account is not None:
                                    account.available_minor += reservation.amount_minor
                                    reservation.status = "released"
                                else:
                                    reservation.status = "consumed"
                            self.audit.append(order_session, f"trace_{operation_id}", record.user_id,
                                              "merchant.order.settled",
                                              {"order_id": order_id, "status": merchant.status,
                                               "reservation_id": reservation.id})
            if record.capability_id in {'X03', 'X04'} and data.get('status') in {'failed', 'paused'}:
                status = str(data['status'])
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
        from agentbank.workflow.planner import BusinessPlanService
        BusinessPlanService.refresh_waiting_plans(self.database.sessions())
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
                stage = str(data.get("application_stage", "application"))
                next_state = {"application": ("reviewing", "review"),
                              "review": ("production", "production"),
                              "production": ("active", "delivered")}.get(stage)
                if next_state is None:
                    continue
                card_status, application_stage = next_state
                data["status"] = card_status
                data["application_stage"] = application_stage
                record.status = "succeeded" if card_status == "active" else "pending"
                card_id = str(data.get("card_id", ""))
                application = session.scalar(select(CardApplication).where(CardApplication.card_id == card_id))
                card = session.get(Card, card_id)
                if application is not None:
                    application.status = card_status
                if card is not None:
                    card.status = card_status
                record.data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                self.audit.append(session, f"trace_{record.id}", record.user_id, "card.application.progressed",
                                  {"record_id": record.id, "status": card_status, "stage": application_stage})
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
            return profile is not None and profile.expires_on >= self.clock.business_date() and int(product_risk[1:]) <= int(profile.level[1:])

    @staticmethod
    def _product_data(product: Product) -> dict[str, Any]:
        return {"id": product.id, "name": product.name, "risk_level": product.risk_level,
                "term_days": product.term_days, "fee_bps": product.fee_bps, "redemption_days": product.redemption_days}
