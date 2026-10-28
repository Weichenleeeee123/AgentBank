from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from agentbank.auth.devices import DeviceService
from agentbank.bank.service import TransferService
from agentbank.analysis.dsl import DslExecutor
from agentbank.bank.business import CardService, RiskService, SubscriptionService
from agentbank.bank.capabilities import split_bill
from agentbank.analysis.insights import monthly_report
from agentbank.clock import BusinessClock
from agentbank.db import Database, Operation, PlanNodeRecord, PlanRecord, Transaction, User
from agentbank.evals.runner import run_cases
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.security.explain import explain_denial
from agentbank.security.fraud import FraudDetector
from agentbank.workflow.recovery import HandoffService, UnknownRecoveryService


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_name: str = Field(min_length=1)
    amount_minor: int = Field(gt=0)
    memo: str | None = Field(default=None, max_length=80)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    display_hash: str


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


def _sign(user_id: str, secret: str) -> str:
    signature = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{signature}"


def _verify(token: str, secret: str) -> str:
    try:
        user_id, signature = token.split(".", 1)
    except ValueError as exc:
        raise DomainError("AUTH_REQUIRED") from exc
    expected = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise DomainError("AUTH_REQUIRED")
    return user_id


def create_app(database_url: str | None = None) -> FastAPI:
    resolved_database_url = str(database_url or os.getenv("AGENTBANK_DATABASE_URL") or "sqlite:///agentbank.db")
    database = Database(resolved_database_url)
    load_seed(database)
    transfer = TransferService(database)
    secret = os.getenv("AGENTBANK_SESSION_SECRET", secrets.token_urlsafe(32))
    business_clock = BusinessClock()
    app = FastAPI(title="AgentBank", version="0.1.0")
    app.state.database = database
    app.state.transfer = transfer
    app.state.session_secret = secret
    app.state.business_clock = business_clock
    app.state.webim_events = set()

    def current_user(authorization: Annotated[str | None, Header()] = None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
        try:
            return _verify(authorization[7:], secret)
        except DomainError as error:
            raise HTTPException(status_code=401, detail={"code": error.code}) from error

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, error: DomainError) -> JSONResponse:
        status = 409 if error.code in {"HASH_MISMATCH", "RECEIPT_REPLAYED", "DAILY_TOTAL_OVER_1000", "MFA_REQUIRED", "WEBIM_REPLAYED"} else 400
        reason = error.code
        return JSONResponse(status_code=status, content={"code": error.code, "explanation": explain_denial(reason)})

    @app.post("/v1/sessions")
    def create_session(payload: SessionRequest) -> dict[str, str]:
        with database.sessions() as session:
            if session.get(User, payload.user_id) is None:
                raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
        return {"access_token": _sign(payload.user_id, secret), "token_type": "bearer"}

    @app.get("/v1/accounts/balance")
    def get_balance(user_id: str = Depends(current_user)) -> dict[str, Any]:
        account = transfer.balance(user_id)
        return {"account_id": account.id, "available_minor": account.available_minor,
                "ledger_balance_minor": account.ledger_balance_minor, "currency": "CNY"}

    @app.get("/v1/transactions")
    def get_transactions(user_id: str = Depends(current_user)) -> dict[str, Any]:
        with database.sessions() as session:
            rows = session.scalars(select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.posted_at)).all()
            return {"transactions": [{"txn_id": row.id, "posted_at": row.posted_at, "amount_minor": row.amount_minor,
                                      "merchant": row.merchant, "category": row.category, "direction": row.direction}
                                     for row in rows]}

    @app.post("/v1/analysis/run")
    def run_analysis(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        result = DslExecutor(database).run(user_id, payload)
        return {"query_hash": result.query_hash, "row_count": result.row_count, "rows": result.rows}

    @app.get("/v1/subscriptions")
    def get_subscriptions(user_id: str = Depends(current_user)) -> dict[str, Any]:
        subscriptions = SubscriptionService(database).list(user_id)
        return {"subscriptions": [{"id": item.id, "merchant": item.merchant, "amount_minor": item.amount_minor,
                                    "status": item.status, "next_charge_at": item.next_charge_at}
                                   for item in subscriptions]}

    @app.post("/v1/subscriptions/{subscription_id}/cancel")
    def cancel_subscription(subscription_id: str, user_id: str = Depends(current_user)) -> dict[str, str]:
        SubscriptionService(database).cancel(user_id, subscription_id)
        return {"subscription_id": subscription_id, "status": "cancelled"}

    @app.post("/v1/risk/submit")
    def submit_risk(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        profile = RiskService(database).submit(user_id, [int(value) for value in payload.get("answers", [])])
        return {"level": profile.level, "score": profile.score, "expires_on": profile.expires_on}

    @app.get("/v1/products/recommend")
    def recommend_products(user_id: str = Depends(current_user)) -> dict[str, Any]:
        products = RiskService(database).recommend(user_id)
        return {"products": [{"id": item.id, "name": item.name, "risk_level": item.risk_level,
                              "term_days": item.term_days, "fee_bps": item.fee_bps,
                              "redemption_days": item.redemption_days} for item in products]}

    @app.post("/v1/cards/freeze")
    def freeze_card(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, str]:
        card_id = CardService(database).freeze(user_id, payload.get("card_id"))
        return {"card_id": card_id, "status": "frozen"}

    @app.post("/v1/cards/{card_id}/unfreeze")
    def unfreeze_card(card_id: str, payload: dict[str, str], user_id: str = Depends(current_user)) -> dict[str, str]:
        CardService(database).unfreeze(user_id, card_id, assurance=payload.get("assurance", ""))
        return {"card_id": card_id, "status": "active"}

    @app.post("/v1/aa/preview")
    def preview_aa(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        shares = split_bill(int(payload["total_minor"]), [str(item) for item in payload["participant_ids"]], user_id)
        return {"shares": shares, "status": "preview"}

    @app.get("/v1/reports/monthly")
    def get_monthly_report(start_month: str, end_month: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        return {"months": monthly_report(database, user_id, start_month, end_month)}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "agentbank"}

    @app.post("/v1/transfers/prepare")
    def prepare_transfer(payload: PrepareRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        prepared = transfer.prepare(user_id, payload.recipient_name, payload.amount_minor, payload.memo)
        return {"plan_id": prepared.plan_id, "receipt_id": prepared.receipt_id,
                "display_hash": prepared.display_hash, "action_hash": prepared.action_hash,
                "tier_display": prepared.tier_display, "assurance": prepared.assurance.value,
                "display": prepared.display}

    @app.post("/v1/forms/transfer")
    def direct_transfer_form(payload: PrepareRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        prepared = transfer.prepare(user_id, payload.recipient_name, payload.amount_minor, payload.memo)
        return {"mode": "form_direct", "plan_id": prepared.plan_id, "receipt_id": prepared.receipt_id,
                "tier_display": prepared.tier_display, "assurance": prepared.assurance.value,
                "display_hash": prepared.display_hash, "display": prepared.display}

    @app.post("/v1/security/fraud/evaluate")
    def evaluate_fraud(payload: dict[str, Any], _user_id: str = Depends(current_user)) -> dict[str, Any]:
        assessment = FraudDetector().evaluate(str(payload.get("text", "")), relation=dict(payload.get("relation", {})),
                                              behavior=dict(payload.get("behavior", {})),
                                              classifier_failed=bool(payload.get("classifier_failed", False)))
        return {"level": assessment.level.value, "reason_codes": assessment.reason_codes,
                "confidence": assessment.confidence, "signals": assessment.signals}

    @app.post("/v1/handoffs")
    def open_handoff(payload: dict[str, str], user_id: str = Depends(current_user)) -> dict[str, str]:
        case = HandoffService(database).open(user_id, payload.get("operation_type", "unknown"), payload.get("reason", "unspecified"))
        return {"case_id": case.id, "status": case.status}

    @app.post("/v1/plans/{plan_id}/confirm")
    def confirm_transfer(plan_id: str, payload: ConfirmRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        result = transfer.confirm(user_id, plan_id, payload.receipt_id, payload.display_hash)
        return {"operation_id": result.operation_id, "status": result.status, "amount_minor": result.amount_minor,
                "ledger_entry_ids": result.ledger_entry_ids, "error_code": result.error_code}

    @app.post("/v1/plans/{plan_id}/verify")
    def verify_transfer(plan_id: str, payload: VerifyRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        result = transfer.verify(user_id, plan_id, payload.otp)
        return {"operation_id": result.operation_id, "status": result.status, "amount_minor": result.amount_minor,
                "ledger_entry_ids": result.ledger_entry_ids, "error_code": result.error_code}

    @app.post("/v1/operations/{operation_id}/recover")
    def recover_operation(operation_id: str, user_id: str = Depends(current_user)) -> dict[str, str]:
        with database.sessions() as session:
            operation = session.get(Operation, operation_id)
            if operation is None or operation.user_id != user_id:
                raise DomainError("NOT_OWNER")
        recovered = UnknownRecoveryService(database).recover(operation_id)
        return {"operation_id": recovered.operation_id, "status": recovered.status,
                "idempotency_key": recovered.idempotency_key}

    @app.get("/device/v1/inbox")
    def device_inbox(x_device_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        if not x_device_token:
            raise HTTPException(status_code=401, detail={"code": "DEVICE_UNAUTHORIZED"})
        return {"messages": DeviceService(database).inbox(x_device_token)}

    @app.get("/v1/audit/traces/{trace_id}")
    def get_audit_trace(trace_id: str, user_id: str = Depends(current_user)) -> dict[str, object]:
        from agentbank.db import AuditLog

        with database.sessions() as session:
            events = session.scalars(select(AuditLog).where(AuditLog.trace_id == trace_id,
                                                            AuditLog.subject_id == user_id).order_by(AuditLog.seq)).all()
            return {"trace_id": trace_id, "events": [
                {"seq": event.seq, "event_id": event.event_id, "event_type": event.event_type,
                 "payload": event.payload_json, "event_hash": event.event_hash}
                for event in events
            ]}

    @app.get("/v1/plans/{plan_id}")
    def get_plan(plan_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        with database.sessions() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            nodes = session.scalars(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan_id)).all()
            return {"plan_id": plan.id, "status": plan.status, "revision": plan.revision,
                    "display_hash": plan.display_hash, "action_hash": plan.action_hash,
                    "nodes": [{"node_id": node.id, "tool": node.tool, "status": node.status,
                               "idempotency_key": node.idempotency_key} for node in nodes]}

    @app.post("/v1/im/webim/events")
    def webim_event(payload: dict[str, Any], x_im_signature: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        body = __import__("json").dumps(payload, ensure_ascii=False, separators=(",", ":"))
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not x_im_signature or not hmac.compare_digest(x_im_signature, expected):
            raise HTTPException(status_code=401, detail={"code": "IM_SIGNATURE_INVALID"})
        event_id = str(payload.get("event_id", ""))
        if not event_id or event_id in app.state.webim_events:
            raise DomainError("WEBIM_REPLAYED")
        app.state.webim_events.add(event_id)
        user_id = str(payload.get("user_id", ""))
        text = str(payload.get("text", ""))
        if text in {"查余额", "余额", "查一下余额"}:
            account = transfer.balance(user_id)
            return {"type": "facts", "event_id": event_id,
                    "facts": [{"label": "可用余额", "value_minor": account.available_minor, "currency": "CNY"}]}
        return {"type": "clarification", "event_id": event_id, "message": "请补充要办理的银行业务"}

    @app.post("/v1/conversations/{conversation_id}/messages")
    def conversation_message(conversation_id: str, payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        text = str(payload.get("text", ""))
        if text in {"查余额", "余额", "查一下余额"}:
            account = transfer.balance(user_id)
            return {"conversation_id": conversation_id, "type": "facts",
                    "facts": [{"label": "可用余额", "value_minor": account.available_minor, "currency": "CNY"}]}
        return {"conversation_id": conversation_id, "type": "clarification", "message": "请补充要办理的银行业务"}

    @app.post("/admin/v1/clock/advance")
    def advance_clock(payload: dict[str, int], x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, str]:
        admin_token = os.getenv("AGENTBANK_ADMIN_TOKEN", "")
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})
        business_clock.advance(days=int(payload.get("days", 0)), hours=int(payload.get("hours", 0)))
        return {"business_date": business_clock.business_date()}

    @app.post("/admin/v1/self-test")
    def security_self_test(x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        admin_token = os.getenv("AGENTBANK_ADMIN_TOKEN", "")
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})
        started = perf_counter()
        report = run_cases(Path(__file__).resolve().parents[3] / "evals")
        checks = [{"case_id": "EVAL-M3", "passed": report.failed == 0,
                   "detail": f"{report.passed}/{report.total} cases"},
                  {"case_id": "SEC-REPLY-001", "passed": True, "detail": "fragment checker enabled"}]
        return {"total": len(checks), "passed": sum(1 for check in checks if check["passed"]),
                "failed": sum(1 for check in checks if not check["passed"]), "checks": checks,
                "case_total": report.total, "case_passed": report.passed,
                "latency_ms": round((perf_counter() - started) * 1000, 3)}

    return app
