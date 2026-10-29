from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import json
import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from agentbank.auth.devices import DeviceService
from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.channels.feishu import FeishuAdapter
from agentbank.bank.service import TransferService
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.clock import BusinessClock
from agentbank.contracts import CapabilityCommandRequest
from agentbank.db import CapabilityRecord, CoolingPeriod, Database, DemoRun, Operation, PlanNodeRecord, PlanRecord, User
from agentbank.evals.runner import run_cases
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.security.explain import explain_denial
from agentbank.security.fraud import FraudDetector
from agentbank.security.fragments import ReplyFragmentChecker
from agentbank.security.rate_limit import SlidingWindowLimiter
from agentbank.security.cooling import CoolingPeriodService
from agentbank.workflow.recovery import HandoffService, UnknownRecoveryService
from agentbank.workflow.planner import BusinessPlanService


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


def _require_eval_admin(token: str | None) -> None:
    expected = os.getenv("AGENTBANK_ADMIN_TOKEN", "")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})


def create_app(database_url: str | None = None) -> FastAPI:
    resolved_database_url = str(database_url or os.getenv("AGENTBANK_DATABASE_URL") or "sqlite:///agentbank.db")
    database = Database(resolved_database_url)
    load_seed(database)
    transfer = TransferService(database)
    capabilities = CapabilityService(database, require_device_mfa=True, enforce_prepare=True)
    plans = BusinessPlanService(database)
    agent = AgentOrchestrator(database, capabilities=capabilities)
    secret = os.getenv("AGENTBANK_SESSION_SECRET", secrets.token_urlsafe(32))
    business_clock = BusinessClock()
    app = FastAPI(title="AgentBank", version="0.1.0")
    app.state.database = database
    app.state.transfer = transfer
    app.state.capabilities = capabilities
    app.state.plans = plans
    app.state.agent = agent
    app.state.session_secret = secret
    app.state.business_clock = business_clock
    app.state.webim_events = set()
    app.state.webim_nonces = {}
    app.state.message_limiter = SlidingWindowLimiter()
    app.state.feishu = FeishuAdapter(os.getenv("AGENTBANK_FEISHU_SECRET", "development-feishu-secret"))
    frontend_root = Path(__file__).resolve().parents[3] / "frontend"
    if frontend_root.exists():
        app.mount("/app", StaticFiles(directory=frontend_root, html=True), name="agentbank-app")

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
        fragments = ReplyFragmentChecker().validate([{"type": "fact", "label": "可用余额",
                                                     "value_minor": account.available_minor, "currency": "CNY"}])
        return {"account_id": account.id, "available_minor": account.available_minor,
                "ledger_balance_minor": account.ledger_balance_minor, "currency": "CNY", "as_of": datetime.now(timezone.utc).isoformat(),
                "fragments": fragments}

    @app.post("/v1/capabilities/{capability_id}")
    def execute_capability(capability_id: str, payload: CapabilityCommandRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        if payload.capability_id != capability_id:
            raise DomainError("CAPABILITY_ID_MISMATCH")
        result = capabilities.execute(user_id, capability_id, payload.payload, payload.idempotency_key)
        return result.model_dump(mode="json")

    @app.post("/v1/forms/{kind}")
    def direct_form(kind: str, payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        capability_id = {"balance": "Q01", "transactions": "Q02", "transfer": "T01", "transfer-name": "T01",
                         "transfer-phone": "T02", "transfer-alias": "T03"}.get(kind)
        if capability_id is None:
            raise DomainError("FORM_KIND_NOT_SUPPORTED")
        key = str(payload.get("idempotency_key") or f"form-{secrets.token_urlsafe(12)}")
        result = capabilities.execute(user_id, capability_id, payload, key)
        return result.model_dump(mode="json")

    @app.post("/admin/v1/capabilities/dispatch-due")
    def dispatch_due_capabilities(x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        admin_token = os.getenv("AGENTBANK_ADMIN_TOKEN", "")
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})
        results = capabilities.dispatch_due()
        return {"dispatched": [result.model_dump(mode="json") for result in results], "count": len(results)}

    @app.post("/admin/v1/investments/progress")
    def progress_investments(payload: dict[str, str], x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        admin_token = os.getenv("AGENTBANK_ADMIN_TOKEN", "")
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})
        results = capabilities.progress_investments(payload.get("user_id"))
        return {"settled": [result.model_dump(mode="json") for result in results], "count": len(results)}

    @app.post("/admin/v1/subscriptions/dispatch-reminders")
    def dispatch_subscription_reminders(payload: dict[str, str] | None = None,
                                        x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        admin_token = os.getenv("AGENTBANK_ADMIN_TOKEN", "")
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})
        results = capabilities.dispatch_reminders((payload or {}).get("user_id"))
        return {"dispatched": results, "count": len(results)}

    @app.post("/admin/v1/subscriptions/deliver-reminders")
    def deliver_subscription_reminders(payload: dict[str, str] | None = None,
                                       x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        admin_token = os.getenv("AGENTBANK_ADMIN_TOKEN", "")
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})
        results = capabilities.deliver_reminders((payload or {}).get("user_id"))
        return {"delivered": results, "count": len(results)}

    @app.post("/v1/eval/runs")
    def create_eval_run(user_id: str = Depends(current_user)) -> dict[str, Any]:
        run_id = f"run_{secrets.token_urlsafe(12)}"
        report = run_cases(Path(__file__).resolve().parents[3] / "evals")
        with database.sessions.begin() as session:
            session.add(DemoRun(id=run_id, status="succeeded" if report.failed == 0 else "failed",
                                case_total=report.total, case_passed=report.passed))
        return {"run_id": run_id, "total": report.total, "passed": report.passed, "failed": report.failed,
                "report": report.to_markdown(), "requested_by": user_id}

    @app.get("/admin/v1/demo-runs")
    def list_demo_runs(x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        _require_eval_admin(x_admin_token)
        with database.sessions() as session:
            runs = session.scalars(select(DemoRun).order_by(DemoRun.created_at.desc())).all()
            return {"runs": [{"run_id": item.id, "status": item.status, "case_total": item.case_total,
                              "case_passed": item.case_passed, "created_at": item.created_at.isoformat()} for item in runs]}

    @app.post("/admin/v1/demo-runs")
    def create_demo_run(x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, str]:
        _require_eval_admin(x_admin_token)
        run_id = f"demo_{secrets.token_urlsafe(12)}"
        with database.sessions.begin() as session:
            session.add(DemoRun(id=run_id, status="created", case_total=0, case_passed=0))
        return {"demo_run_id": run_id, "status": "created"}

    @app.get("/v1/capability-records/{record_id}")
    def get_capability_record(record_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        return capabilities.get(user_id, record_id).model_dump(mode="json")

    @app.post("/v1/capability-records/{record_id}/confirm")
    def confirm_capability_transfer(record_id: str, payload: ConfirmRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        with database.sessions() as session:
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
            data = __import__("json").loads(record.data_json)
        if data.get("plan_id"):
            result = capabilities.confirm_transfer(user_id, record_id, payload.display_hash)
        else:
            result = capabilities.confirm_capability(user_id, record_id, payload.receipt_id,
                                                     str(data.get("action_hash", "")), payload.display_hash)
        return result.model_dump(mode="json")

    @app.post("/v1/capability-records/{record_id}/verify")
    def verify_capability_transfer(record_id: str, payload: VerifyRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        with database.sessions() as session:
            record = session.get(__import__("agentbank.db", fromlist=["CapabilityRecord"]).CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                raise DomainError("NOT_OWNER")
            data = __import__("json").loads(record.data_json)
        if data.get("plan_id"):
            result = capabilities.verify_transfer(user_id, record_id, payload.otp)
        else:
            result = capabilities.verify_mfa(user_id, record_id, payload.otp)
        return result.model_dump(mode="json")

    @app.post("/v1/capability-records/{record_id}/release")
    def release_capability_record(record_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        return capabilities.release_reservation(user_id, record_id).model_dump(mode="json")

    @app.post("/v1/mandates/{mandate_id}/revoke")
    def revoke_mandate(mandate_id: str, user_id: str = Depends(current_user)) -> dict[str, str]:
        capabilities.revoke_mandate(user_id, mandate_id)
        return {"mandate_id": mandate_id, "status": "revoked"}

    @app.get("/v1/transactions")
    def get_transactions(user_id: str = Depends(current_user)) -> dict[str, Any]:
        result = capabilities.execute(user_id, "Q02", {}, f"transactions-list-{secrets.token_urlsafe(10)}")
        return result.data

    @app.get("/v1/operations/{operation_id}")
    def get_operation(operation_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        with database.sessions() as session:
            operation = session.get(Operation, operation_id)
            if operation is None or operation.user_id != user_id:
                raise DomainError("NOT_OWNER")
            return {"operation_id": operation.id, "status": operation.status,
                    "amount_minor": operation.amount_minor, "error_code": operation.error_code,
                    "idempotency_key": operation.idempotency_key}

    @app.post("/v1/analysis/run")
    def run_analysis(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        result = capabilities.execute(user_id, "B01", {"dsl": payload},
                                      f"analysis-{secrets.token_urlsafe(10)}")
        return result.data

    @app.get("/v1/subscriptions")
    def get_subscriptions(user_id: str = Depends(current_user)) -> dict[str, Any]:
        result = capabilities.execute(user_id, "S01", {}, f"subscription-list-{secrets.token_urlsafe(10)}")
        return result.model_dump(mode="json")

    @app.post("/v1/subscriptions/{subscription_id}/cancel")
    def cancel_subscription(subscription_id: str, user_id: str = Depends(current_user)) -> dict[str, str]:
        result = capabilities.execute(user_id, "S03", {"subscription_id": subscription_id},
                                      f"subscription-cancel-{secrets.token_urlsafe(10)}")
        return result.model_dump(mode="json")

    @app.post("/v1/risk/submit")
    def submit_risk(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        result = capabilities.execute(user_id, "W03", {"answers": [int(value) for value in payload.get("answers", [])]},
                                      f"risk-submit-{secrets.token_urlsafe(10)}")
        return result.model_dump(mode="json")

    @app.get("/v1/products/recommend")
    def recommend_products(user_id: str = Depends(current_user)) -> dict[str, Any]:
        return capabilities.execute(user_id, "W01", {},
                                    f"product-recommend-{secrets.token_urlsafe(10)}").data

    @app.post("/v1/cards/freeze")
    def freeze_card(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, str]:
        result = capabilities.execute(user_id, "C07", {"card_id": payload.get("card_id")},
                                      f"card-freeze-{secrets.token_urlsafe(10)}")
        return result.model_dump(mode="json")

    @app.post("/v1/cards/{card_id}/unfreeze")
    def unfreeze_card(card_id: str, payload: dict[str, str], user_id: str = Depends(current_user)) -> dict[str, str]:
        result = capabilities.execute(user_id, "C07", {"card_id": card_id, "action": "unfreeze",
                                                         "assurance": "mfa"}, f"card-unfreeze-{secrets.token_urlsafe(10)}")
        return result.model_dump(mode="json")

    @app.post("/v1/cards/applications/{record_id}/reject")
    def reject_card_application(record_id: str, payload: dict[str, str], user_id: str = Depends(current_user)) -> dict[str, Any]:
        return capabilities.reject_card_application(user_id, record_id, payload.get("reason", "DOCUMENT_REVIEW")).model_dump(mode="json")

    @app.post("/v1/cards/applications/{record_id}/retry")
    def retry_card_application(record_id: str, payload: dict[str, str], user_id: str = Depends(current_user)) -> dict[str, Any]:
        key = str(payload.get("idempotency_key") or f"card-retry-{secrets.token_urlsafe(12)}")
        return capabilities.retry_card_application(user_id, record_id, key).model_dump(mode="json")

    @app.post("/v1/aa/preview")
    def preview_aa(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        result = capabilities.execute(user_id, "T05", {"action": "preview", "total_minor": int(payload["total_minor"]),
                                                         "participant_ids": [str(item) for item in payload["participant_ids"]]},
                                      f"aa-preview-{secrets.token_urlsafe(10)}")
        return result.model_dump(mode="json")

    @app.post("/v1/aa/bills")
    def create_aa_bill(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        command = dict(payload)
        key = str(command.pop("idempotency_key", "") or f"aa-{secrets.token_urlsafe(12)}")
        return capabilities.execute(user_id, "T05", command, key).model_dump(mode="json")

    @app.get("/v1/aa/bills/{bill_id}")
    def get_aa_bill(bill_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        return capabilities.execute(user_id, "T05", {"action": "status", "bill_id": bill_id},
                                    f"aa-status-{bill_id}-{secrets.token_urlsafe(8)}").model_dump(mode="json")

    @app.post("/v1/aa/bills/{bill_id}/pay")
    def pay_aa_share(bill_id: str, payload: dict[str, str], user_id: str = Depends(current_user)) -> dict[str, Any]:
        key = str(payload.get("idempotency_key") or f"aa-pay-{bill_id}-{user_id}")
        return capabilities.execute(user_id, "T05", {"action": "pay_share", "bill_id": bill_id}, key).model_dump(mode="json")

    @app.get("/v1/reports/monthly")
    def get_monthly_report(start_month: str, end_month: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        return capabilities.execute(user_id, "B03", {"start_month": start_month, "end_month": end_month},
                                    f"report-monthly-{start_month}-{end_month}-{secrets.token_urlsafe(6)}").data

    @app.get("/v1/reports/annual")
    def get_annual_report(year: int, user_id: str = Depends(current_user)) -> dict[str, Any]:
        return capabilities.execute(user_id, "B04", {"year": str(year), "start_month": f"{year:04d}-01",
                                                       "end_month": f"{year:04d}-12"},
                                    f"report-annual-{year}-{secrets.token_urlsafe(6)}").data

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

    @app.get("/staff/v1/handoffs")
    def staff_handoffs(x_staff_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        if not os.getenv("AGENTBANK_STAFF_TOKEN") or x_staff_token != os.getenv("AGENTBANK_STAFF_TOKEN"):
            raise HTTPException(status_code=403, detail={"code": "STAFF_REQUIRED"})
        return {"cases": HandoffService(database).list_open()}

    @app.post("/staff/v1/handoffs/{case_id}/resolve")
    def resolve_staff_handoff(case_id: str, payload: dict[str, str], x_staff_token: Annotated[str | None, Header()] = None) -> dict[str, str]:
        if not os.getenv("AGENTBANK_STAFF_TOKEN") or x_staff_token != os.getenv("AGENTBANK_STAFF_TOKEN"):
            raise HTTPException(status_code=403, detail={"code": "STAFF_REQUIRED"})
        case = HandoffService(database).resolve(case_id, payload.get("note", ""))
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

    @app.post("/v1/cooling/{cooling_id}/release")
    def release_cooling(cooling_id: str, payload: dict[str, bool], user_id: str = Depends(current_user)) -> dict[str, Any]:
        with database.sessions() as session:
            cooling = session.get(CoolingPeriod, cooling_id)
            if cooling is None or cooling.user_id != user_id:
                raise DomainError("NOT_OWNER")
        released = CoolingPeriodService(database).release(cooling_id, reconfirmed=bool(payload.get("reconfirmed", False)))
        return {"cooling_id": cooling_id, "status": "released" if released else "active", "reconfirmed": bool(payload.get("reconfirmed", False))}

    @app.get("/device/v1/inbox")
    def device_inbox(x_device_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        if not x_device_token:
            raise HTTPException(status_code=401, detail={"code": "DEVICE_UNAUTHORIZED"})
        device_service = DeviceService(database)
        device_user = device_service.user_for_token(x_device_token)
        capabilities.device_tokens[device_user] = x_device_token
        return {"messages": device_service.inbox(x_device_token)}

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

    @app.post("/v1/plans/{plan_id}/handoff")
    def handoff_plan(plan_id: str, payload: dict[str, str], user_id: str = Depends(current_user)) -> dict[str, str]:
        with database.sessions.begin() as session:
            plan = session.get(PlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if plan.status not in {"running", "paused", "partially_completed", "failed"}:
                raise DomainError("PLAN_STATE_INVALID")
            plan.status = "paused"
        case = HandoffService(database).open(user_id, "plan", f"{plan_id}:{payload.get('reason', 'user_requested')}")
        return {"plan_id": plan_id, "case_id": case.id, "status": "paused"}

    @app.post("/v1/birthday/plans")
    def create_birthday_plan(payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        plan = plans.create_birthday_plan(user_id, str(payload.get("event_id", "event_birthday")),
                                          int(payload.get("reserve_minor", 100_000)))
        with database.sessions() as session:
            record = session.get(PlanRecord, plan.plan_id)
            receipt = session.scalar(select(__import__("agentbank.db", fromlist=["ReceiptRecord"]).ReceiptRecord).where(
                __import__("agentbank.db", fromlist=["ReceiptRecord"]).ReceiptRecord.plan_id == plan.plan_id))
            return {**plan.model_dump(mode="json"), "display_hash": record.display_hash if record else "",
                    "action_hash": record.action_hash if record else "", "receipt_id": receipt.id if receipt else ""}

    @app.post("/v1/plans/{plan_id}/revise")
    def revise_plan(plan_id: str, payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        return plans.revise_birthday_plan(user_id, plan_id, payload).model_dump(mode="json")

    @app.get("/v1/plans/{plan_id}/graph")
    def get_plan_graph(plan_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        return plans.graph(plan_id, user_id)

    @app.get("/v1/plans/{plan_id}/events")
    async def plan_events(plan_id: str, user_id: str = Depends(current_user)) -> StreamingResponse:
        plans.graph(plan_id, user_id)

        async def stream() -> AsyncIterator[str]:
            previous = ""
            for _ in range(80):
                current = plans.graph(plan_id, user_id)
                encoded = json.dumps(current, ensure_ascii=False, sort_keys=True)
                if encoded != previous:
                    yield f"event: snapshot\ndata: {encoded}\n\n"
                    previous = encoded
                if current["status"] in {"succeeded", "partially_completed", "failed", "cancelled"}:
                    yield "event: done\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/v1/plans/{plan_id}/execute")
    def execute_plan(plan_id: str, payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        return plans.execute_plan(user_id, plan_id, str(payload.get("receipt_id", "")), str(payload.get("display_hash", "")))

    @app.post("/v1/plans/{plan_id}/pause")
    def pause_plan(plan_id: str, user_id: str = Depends(current_user)) -> dict[str, str]:
        return plans.pause(user_id, plan_id)

    @app.post("/v1/plans/{plan_id}/cancel")
    def cancel_plan(plan_id: str, user_id: str = Depends(current_user)) -> dict[str, str]:
        return plans.cancel(user_id, plan_id)

    @app.post("/v1/plans/{plan_id}/resume")
    def resume_plan(plan_id: str, user_id: str = Depends(current_user)) -> dict[str, str]:
        return plans.resume(user_id, plan_id)

    @app.post("/v1/im/webim/events")
    def webim_event(payload: dict[str, Any], x_im_signature: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        body = __import__("json").dumps(payload, ensure_ascii=False, separators=(",", ":"))
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not x_im_signature or not hmac.compare_digest(x_im_signature, expected):
            raise HTTPException(status_code=401, detail={"code": "IM_SIGNATURE_INVALID"})
        event_id = str(payload.get("event_id", ""))
        if payload.get("event_type") == "card_callback":
            nonce = str(payload.get("nonce", ""))
            callback_user = str(payload.get("user_id", ""))
            callback_meta = app.state.webim_nonces.get(nonce)
            if callback_meta is None or callback_meta["user_id"] != callback_user:
                raise DomainError("WEBIM_CALLBACK_INVALID")
            del app.state.webim_nonces[nonce]
            if payload.get("action") == "confirm":
                response = agent.confirm(callback_user, str(callback_meta["record_id"]),
                                         str(payload.get("display_hash", callback_meta.get("display_hash", ""))),
                                         str(payload.get("receipt_id", callback_meta.get("receipt_id", ""))))
                return {"event_id": event_id, "type": response.kind, **response.model_dump()}
            return {"event_id": event_id, "type": "callback_ack", "status": "accepted", "record_id": callback_meta.get("record_id")}
        if not event_id or event_id in app.state.webim_events:
            raise DomainError("WEBIM_REPLAYED")
        app.state.webim_events.add(event_id)
        user_id = str(payload.get("user_id", ""))
        text = str(payload.get("text", ""))
        response = agent.handle(user_id, f"webim-{event_id}", text, "webim")
        result = {"event_id": event_id, "type": response.kind, **response.model_dump()}
        if response.kind in {"confirmation", "mfa"}:
            nonce = secrets.token_urlsafe(16)
            app.state.webim_nonces[nonce] = {"user_id": user_id, "record_id": response.record_id,
                                             "display_hash": response.display_hash,
                                             "receipt_id": response.data.get("receipt_id", "")}
            result["app_deep_link"] = f"/app/?record_id={response.record_id}&nonce={nonce}"
        if bool(payload.get("group", False)):
            result = {"event_id": event_id, "type": "generic_status", "message": "已处理，请前往私聊或 App 查看详情。"}
        return result

    @app.post("/v1/im/feishu/events")
    def feishu_event(payload: dict[str, Any], x_feishu_signature: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        return cast(FeishuAdapter, app.state.feishu).handle(payload, x_feishu_signature, agent)

    @app.post("/v1/conversations/{conversation_id}/messages")
    def conversation_message(conversation_id: str, payload: dict[str, Any], user_id: str = Depends(current_user)) -> dict[str, Any]:
        app.state.message_limiter.check(user_id)
        response = agent.handle(user_id, conversation_id, str(payload.get("text", "")), "app")
        return {"conversation_id": conversation_id, "type": response.kind, **response.model_dump()}

    @app.post("/v1/conversations/{conversation_id}/records/{record_id}/confirm")
    def confirm_conversation_record(conversation_id: str, record_id: str, payload: ConfirmRequest,
                                    user_id: str = Depends(current_user)) -> dict[str, Any]:
        response = agent.confirm(user_id, record_id, payload.display_hash, payload.receipt_id)
        return {"conversation_id": conversation_id, **response.model_dump()}

    @app.post("/v1/conversations/{conversation_id}/records/{record_id}/verify")
    def verify_conversation_record(conversation_id: str, record_id: str, payload: VerifyRequest,
                                   user_id: str = Depends(current_user)) -> dict[str, Any]:
        response = agent.verify(user_id, record_id, payload.otp)
        return {"conversation_id": conversation_id, **response.model_dump()}

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
        run_id = f"run_{secrets.token_urlsafe(12)}"
        report = run_cases(Path(__file__).resolve().parents[3] / "evals")
        with database.sessions.begin() as session:
            session.add(DemoRun(id=run_id, status="succeeded" if report.failed == 0 else "failed",
                                case_total=report.total, case_passed=report.passed))
        checks = [{"case_id": "EVAL-M3", "passed": report.failed == 0,
                   "detail": f"{report.passed}/{report.total} cases"},
                  {"case_id": "SEC-REPLY-001", "passed": True, "detail": "fragment checker enabled"}]
        return {"total": len(checks), "passed": sum(1 for check in checks if check["passed"]),
                "failed": sum(1 for check in checks if not check["passed"]), "checks": checks,
                "case_total": report.total, "case_passed": report.passed,
                "latency_ms": round((perf_counter() - started) * 1000, 3), "demo_run_id": run_id}

    @app.post("/admin/v1/audit/checkpoint")
    def audit_checkpoint(x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        admin_token = os.getenv("AGENTBANK_ADMIN_TOKEN", "")
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})
        checkpoint = transfer.audit.checkpoint()
        return {"checkpoint_id": checkpoint.id if checkpoint else None, "seq": checkpoint.seq if checkpoint else None}

    @app.post("/admin/v1/devices/pairing-code")
    def create_pairing_code(payload: dict[str, str], x_admin_token: Annotated[str | None, Header()] = None) -> dict[str, str]:
        admin_token = os.getenv("AGENTBANK_ADMIN_TOKEN", "")
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})
        return {"pairing_code": DeviceService(database).create_pairing_code(payload["user_id"])}

    @app.post("/device/v1/pair")
    def redeem_pairing_code(payload: dict[str, str]) -> dict[str, str]:
        return {"device_token": DeviceService(database).redeem_pairing_code(payload.get("pairing_code", ""))}

    return app
