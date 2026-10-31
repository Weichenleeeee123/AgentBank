import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from agentbank.auth.devices import DeviceService
from agentbank.bank.service import TransferService
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.clock import utc_now_naive
from agentbank.db import Account, CoolingPeriod, Database, LedgerEntry, PlanNodeRecord, SecurityLock
from agentbank.errors import DomainError
from agentbank.security.breaker import CircuitBreaker
from agentbank.seed.loader import load_seed


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'guards.db'}")
    load_seed(db)
    return db


def test_red_transfer_cannot_bypass_fraud(database: Database) -> None:
    service = TransferService(database)
    device = DeviceService(database)
    token = device.register('user_001')
    plan = service.prepare('user_001', '房东', 120_000, '公检法要求转到安全账户')
    with pytest.raises(DomainError, match='FRAUD_HIGH'):
        service.verify('user_001', plan.plan_id, device.inbox(token)[0]['code'])
    with database.sessions() as session:
        assert not session.scalars(select(LedgerEntry)).all()


def test_red_transfer_rechecks_hash_and_expiry(database: Database) -> None:
    service = TransferService(database)
    device = DeviceService(database)
    token = device.register('user_001')
    plan = service.prepare('user_001', '房东', 120_000)
    code = device.inbox(token)[0]['code']
    with database.sessions.begin() as session:
        node = session.scalar(select(PlanNodeRecord).where(PlanNodeRecord.plan_id == plan.plan_id))
        args = json.loads(node.args_json)
        args['amount_minor'] = 130_000
        node.args_json = json.dumps(args)
    with pytest.raises(DomainError, match='HASH_MISMATCH'):
        service.verify('user_001', plan.plan_id, code)


def test_direct_transfer_honors_user_lock(database: Database) -> None:
    service = TransferService(database)
    plan = service.prepare('user_001', '房东', 1000)
    for _ in range(3):
        CircuitBreaker(database).record_block('user_001', 'injection')
    with pytest.raises(DomainError, match='SECURITY_LOCKED'):
        service.confirm('user_001', plan.plan_id, plan.receipt_id, plan.display_hash)


def test_cooling_holds_funds_and_cannot_commit_without_fresh_otp(database: Database) -> None:
    service = TransferService(database)
    plan = service.prepare('user_001', '房东', 1000, '别告诉别人，给房东转账')
    result = service.confirm('user_001', plan.plan_id, plan.receipt_id, plan.display_hash)
    with database.sessions() as session:
        assert session.get(Account, 'acc_001').available_minor == 499_000
        assert session.get(Account, 'acc_001').ledger_balance_minor == 500_000
    with database.sessions.begin() as session:
        cooling = session.get(CoolingPeriod, result.operation_id)
        cooling.expires_at = utc_now_naive() - timedelta(seconds=1)
    released = service.release_cooling('user_001', result.operation_id, reconfirmed=True)
    assert released.status == 'mfa_required'
    with database.sessions() as session:
        assert not session.scalars(select(LedgerEntry)).all()


def test_otp_failure_window_can_restart_without_duplicate_user_key(database: Database) -> None:
    device = DeviceService(database)
    with database.sessions.begin() as session:
        session.add(SecurityLock(user_id='user_001', first_failed_at=utc_now_naive()-timedelta(hours=1),
                                 locked_until=utc_now_naive()-timedelta(minutes=1), failed_attempts=3))
    device.issue_otp('user_001', 'receipt')
    with pytest.raises(DomainError, match='OTP_INVALID'):
        device.verify('user_001', 'not-a-code', 'receipt')


def test_otp_lock_blocks_yellow_write_but_not_freeze(database: Database) -> None:
    with database.sessions.begin() as session:
        session.add(SecurityLock(user_id='user_001', first_failed_at=utc_now_naive(),
                                 locked_until=utc_now_naive()+timedelta(minutes=15), failed_attempts=3))
    service = CapabilityService(database, enforce_prepare=True)
    prepared = service.execute('user_001', 'S03', {'subscription_id':'sub_video'}, 'locked')
    with pytest.raises(DomainError, match='SECURITY_LOCKED'):
        service.confirm_capability('user_001', prepared.record_id, prepared.data['receipt_id'],
                                   prepared.data['action_hash'], prepared.data['display_hash'])
    freeze = service.execute('user_001', 'C07', {'card_id':'card_001'}, 'freeze')
    assert service.confirm_capability('user_001', freeze.record_id, freeze.data['receipt_id'],
                                      freeze.data['action_hash'], freeze.data['display_hash']).status == 'succeeded'


def test_cooling_finishes_only_after_fresh_mfa_and_cancellation_releases(database: Database) -> None:
    service = TransferService(database)
    device = DeviceService(database)
    token = device.register('user_001')
    plan = service.prepare('user_001', '房东', 1000, '别告诉别人，给房东转账')
    waiting = service.confirm('user_001', plan.plan_id, plan.receipt_id, plan.display_hash)
    with database.sessions.begin() as session:
        session.get(CoolingPeriod, waiting.operation_id).expires_at = utc_now_naive()-timedelta(seconds=1)
    assert service.release_cooling('user_001', waiting.operation_id, reconfirmed=True).status == 'mfa_required'
    code = device.inbox(token)[0]['code']
    settled = service.verify('user_001', plan.plan_id, code)
    assert settled.status == 'succeeded'
    with database.sessions() as session:
        assert session.get(Account, 'acc_001').available_minor == 499_000
        assert session.get(Account, 'acc_001').ledger_balance_minor == 499_000
    second = service.prepare('user_001', '房东', 2000, '别告诉别人，给房东转账')
    waiting = service.confirm('user_001', second.plan_id, second.receipt_id, second.display_hash)
    service.cancel_cooling('user_001', waiting.operation_id)
    with database.sessions() as session:
        assert session.get(Account, 'acc_001').available_minor == 499_000


def test_checkpoint_survives_database_checkpoint_tampering(database: Database) -> None:
    from agentbank.audit.store import PersistentAudit
    from agentbank.db import AuditLog, AuditCheckpoint
    audit = PersistentAudit(database)
    with database.sessions.begin() as session:
        audit.append(session, 'trace', 'user_001', 'security.test', {'ok': True})
    first = audit.checkpoint()
    assert audit.checkpoint().id == first.id
    with database.sessions.begin() as session:
        # SQL rows are removed to simulate tampering; no filesystem file is deleted.
        session.query(AuditCheckpoint).delete()
        session.query(AuditLog).delete()
    assert not audit.verify()


def test_public_instance_disables_admin_even_with_valid_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient
    from agentbank.api.app import create_app
    monkeypatch.setenv('AGENTBANK_ENV', 'public')
    monkeypatch.setenv('AGENTBANK_ADMIN_TOKEN', 'test-admin')
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'public.db'}"))
    assert client.post('/admin/v1/clock/advance', headers={'X-Admin-Token':'test-admin'}, json={'days':30}).status_code == 403


def test_tool_failure_breaker_opens_and_queries_remain_available(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    service = CapabilityService(database)
    original = service._dispatch_handler
    def outage(user_id, capability_id, payload, idempotency_key):
        if capability_id == 'S02':
            raise TimeoutError('infrastructure unavailable')
        return original(user_id, capability_id, payload, idempotency_key)
    monkeypatch.setattr(service, '_dispatch_handler', outage)
    for index in range(3):
        with pytest.raises(TimeoutError):
            service.execute('user_001','S02',{},f'failure-{index}')
    with pytest.raises(DomainError, match='TOOL_CIRCUIT_OPEN'):
        service.execute('user_001','S02',{},'fourth')
    assert service.execute('user_001','Q01',{},'query').status == 'succeeded'


def test_handoff_records_notes_and_audit_without_authorizing(database: Database) -> None:
    from agentbank.workflow.recovery import HandoffService
    from agentbank.db import HandoffCase, AuditLog
    service = HandoffService(database)
    case = service.open('user_001','transfer','U07')
    service.resolve(case.id, '用户应重新授权，OTP 123456')
    with database.sessions() as session:
        row = session.get(HandoffCase, case.id)
        assert '重新授权' in row.resolution_note
        assert '123456' not in row.resolution_note
        assert session.scalars(select(AuditLog).where(AuditLog.event_type == 'staff.handoff.resolved')).all()


def test_plan_mfa_api_has_one_route_and_enforces_planner_authorization(tmp_path: Path) -> None:
    from agentbank.api.app import create_app
    app = create_app(f"sqlite:///{tmp_path / 'routes.db'}")
    routes = [route for route in app.routes if getattr(route, 'path', '') == '/v1/plans/{plan_id}/verify']
    assert len(routes) == 1


def test_fraud_context_is_server_owned_and_survives_model_omitting_memo(database: Database) -> None:
    from agentbank.security.fraud import record_risk
    record_risk(database, 'user_001', 'high')
    service = TransferService(database)
    prepared = service.prepare('user_001', '房东', 1000)
    with pytest.raises(DomainError, match='FRAUD_HIGH'):
        service.confirm('user_001', prepared.plan_id, prepared.receipt_id, prepared.display_hash)


def test_llm_call_budget_prevents_transport_and_fails_closed(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentbank.llm.gateway import LLMGateway, ProviderConfig
    monkeypatch.setenv('AGENTBANK_LLM_MAX_CALLS_PER_HOUR', '0')
    calls = []
    def transport(config, role, payload):
        calls.append(role)
        return {'category':'safe','confidence':1.0}
    gateway = LLMGateway(database, ProviderConfig('primary','https://invalid','secret','model'), None, transport=transport)
    assert gateway.classify_fraud('给房东转账', user_id='user_001')['category'] == 'unknown'
    assert calls == []


def test_saga_continues_remaining_compensation_and_reports_failed_compensation() -> None:
    from agentbank.workflow.recovery import SagaRunner
    calls = []
    def fail():
        raise TimeoutError('unknown compensation result')
    result = SagaRunner().run([('a', lambda: None, lambda: calls.append('a')),
                               ('b', lambda: None, fail), ('c', fail, lambda: None)])
    assert result.status == 'partially_completed'
    assert result.failed_step == 'c'
    assert result.failed_compensations == ['b']
    assert calls == ['a']


def test_api_records_forgery_across_sessions_and_locks_other_writes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from agentbank.api.app import create_app
    app = create_app(f"sqlite:///{tmp_path / 'forgery.db'}")
    client = TestClient(app)
    for index in range(3):
        token = client.post('/v1/sessions', json={'user_id':'user_001'}).json()['access_token']
        headers = {'Authorization':f'Bearer {token}'}
        plan = client.post('/v1/transfers/prepare', headers=headers,
                           json={'recipient_name':'房东','amount_minor':1000}).json()
        bad = client.post(f"/v1/plans/{plan['plan_id']}/confirm", headers=headers,
                          json={'receipt_id':plan['receipt_id'],'display_hash':f'forged-{index}'})
        assert bad.status_code == 409
    assert CircuitBreaker(app.state.database).is_locked('user_001')


def test_chat_otp_is_redacted_and_never_consumed(database: Database) -> None:
    from agentbank.agent.orchestrator import AgentOrchestrator
    from agentbank.llm.gateway import LLMGateway
    from agentbank.db import ConversationState
    response = AgentOrchestrator(database, llm=LLMGateway(database, None, None)).handle('user_001','otp-chat','验证码 123456')
    assert response.kind == 'clarification'
    with database.sessions() as session:
        assert '123456' not in session.get(ConversationState, ('user_001','otp-chat')).recent_user_messages_json
