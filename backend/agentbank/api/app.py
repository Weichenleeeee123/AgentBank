from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from agentbank.auth.devices import DeviceService
from agentbank.bank.service import TransferService
from agentbank.db import Database, User
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


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
    app = FastAPI(title="AgentBank", version="0.1.0")
    app.state.database = database
    app.state.transfer = transfer

    def current_user(authorization: Annotated[str | None, Header()] = None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
        try:
            return _verify(authorization[7:], secret)
        except DomainError as error:
            raise HTTPException(status_code=401, detail={"code": error.code}) from error

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, error: DomainError) -> JSONResponse:
        status = 409 if error.code in {"HASH_MISMATCH", "RECEIPT_REPLAYED", "DAILY_TOTAL_OVER_1000", "MFA_REQUIRED"} else 400
        return JSONResponse(status_code=status, content={"code": error.code})

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

    return app
