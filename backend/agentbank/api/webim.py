"""Simulated browser IM gateway. Signing credentials never leave the server."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from typing import Annotated, Any, Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agentbank.audit.store import PersistentAudit
from agentbank.clock import utc_now_naive
from agentbank.db import Database, ImDelivery, ImSession
from agentbank.errors import DomainError


class ImLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: Literal['demo_im_001', 'demo_im_002']


class ImBind(BaseModel):
    model_config = ConfigDict(extra="forbid")
    im_token: str = Field(min_length=16, max_length=200)


class ImMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(default='main', min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)
    group: bool = False


class ImCallback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=100)
    nonce: str = Field(min_length=16, max_length=200)
    action: Literal['confirm'] = 'confirm'
    group: bool = False


def register_webim_routes(app: FastAPI, database: Database, current_user: Callable[..., str],
                         dispatch: Callable[..., dict[str, Any]]) -> None:
    def get_session(raw: str | None) -> ImSession:
        if not raw:
            raise HTTPException(status_code=401, detail={'code': 'IM_AUTH_REQUIRED'})
        with database.sessions() as session:
            record = session.get(ImSession, hashlib.sha256(raw.encode()).hexdigest())
            if record is None or record.expires_at <= utc_now_naive():
                raise HTTPException(status_code=401, detail={'code': 'IM_AUTH_REQUIRED'})
            return record

    def bound_session(x_im_token: Annotated[str | None, Header()] = None) -> ImSession:
        record = get_session(x_im_token)
        if not record.user_id:
            raise HTTPException(status_code=403, detail={'code': 'IM_BINDING_REQUIRED'})
        return record

    def sign_and_dispatch(payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        signature = hmac.new(app.state.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return dispatch(payload, signature)

    @app.post('/v1/im/webim/sessions')
    def login(payload: ImLogin) -> dict[str, Any]:
        raw = secrets.token_urlsafe(32)
        with database.atomic() as session:
            session.add(ImSession(token_hash=hashlib.sha256(raw.encode()).hexdigest(), identity=payload.identity,
                                  user_id=None, expires_at=utc_now_naive() + timedelta(hours=8)))
        return {'access_token': raw, 'identity': payload.identity, 'bound_user_id': None}

    @app.get('/v1/im/webim/session')
    def status(x_im_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        record = get_session(x_im_token)
        return {'identity': record.identity, 'bound_user_id': record.user_id}

    @app.post('/v1/im/webim/bind')
    def bind(payload: ImBind, user_id: str = Depends(current_user)) -> dict[str, Any]:
        record = get_session(payload.im_token)
        with database.atomic() as session:
            stored = session.get(ImSession, record.token_hash)
            assert stored is not None
            # Binding applies only to this independently issued IM session.
            stored.user_id = user_id
            PersistentAudit(database).append(session, f'trace_im_{secrets.token_hex(8)}', user_id,
                                             'channel.im.bound', {'identity': record.identity})
        return {'identity': record.identity, 'bound_user_id': user_id}

    @app.post('/v1/im/webim/messages')
    def message(payload: ImMessage, record: ImSession = Depends(bound_session)) -> dict[str, Any]:
        event_key = hashlib.sha256(f'{record.token_hash}:{payload.event_id}'.encode()).hexdigest()
        chat = hashlib.sha256(f'{record.token_hash}:{payload.conversation_id}'.encode()).hexdigest()
        request_hash = hashlib.sha256(json.dumps([record.user_id, payload.model_dump()], sort_keys=True).encode()).hexdigest()
        with database.atomic() as session:
            prior = session.get(ImDelivery, (record.token_hash, payload.event_id))
            if prior is not None:
                if prior.request_hash != request_hash:
                    raise DomainError('IDEMPOTENCY_CONFLICT')
                if not prior.response_json:
                    raise DomainError('MESSAGE_OUTCOME_UNKNOWN')
                return dict(json.loads(prior.response_json))
            session.add(ImDelivery(token_hash=record.token_hash, event_id=payload.event_id,
                                   request_hash=request_hash, response_json=''))
        result = sign_and_dispatch({'event_id': f'browser-{event_key}', 'user_id': record.user_id,
                                    'conversation_id': chat, 'text': payload.text, 'group': payload.group,
                                    'channel_session': record.token_hash})
        with database.atomic() as session:
            delivery = session.get(ImDelivery, (record.token_hash, payload.event_id))
            assert delivery is not None
            delivery.response_json = json.dumps(result, ensure_ascii=False)
        return result

    @app.post('/v1/im/webim/callbacks')
    def callback(payload: ImCallback, record: ImSession = Depends(bound_session)) -> dict[str, Any]:
        event_key = hashlib.sha256(f'{record.token_hash}:{payload.event_id}'.encode()).hexdigest()
        return sign_and_dispatch({'event_id': f'browser-{event_key}', 'event_type': 'card_callback',
                                  'user_id': record.user_id, 'nonce': payload.nonce,
                                  'action': payload.action, 'group': payload.group,
                                  'channel_session': record.token_hash})
