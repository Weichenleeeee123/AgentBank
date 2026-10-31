import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def _client(tmp_path: Path):
    app = create_app(f"sqlite:///{tmp_path / 'messages.db'}")
    client = TestClient(app)
    token = client.post('/v1/sessions', json={'user_id': 'user_001'}).json()['access_token']
    return app, client, {'Authorization': f'Bearer {token}'}


def _say(client, headers, text, message_id=None, conversation='same'):
    payload = {'text': text}
    if message_id is not None:
        payload['message_id'] = message_id
    return client.post(f'/v1/conversations/{conversation}/messages', headers=headers, json=payload)


def test_new_repeated_query_reads_current_balance_after_transfer(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    first = _say(client, headers, '查余额').json()
    transfer = _say(client, headers, '给房东转20元').json()
    response = client.post(f"/v1/capability-records/{transfer['record_id']}/confirm", headers=headers,
                          json={'receipt_id': transfer['data']['receipt_id'], 'display_hash': transfer['display_hash']})
    assert response.json()['data']['transfer_result']['status'] == 'succeeded'
    second = _say(client, headers, '查余额').json()
    assert second['data']['available_minor'] == first['data']['available_minor'] - 2000
    assert second['record_id'] != first['record_id']


def test_message_retry_is_same_operation_but_new_identical_message_is_new(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    first = _say(client, headers, '给房东转20元', 'msg-1').json()
    retry = _say(client, headers, '给房东转20元', 'msg-1').json()
    assert retry == first
    second = _say(client, headers, '给房东转20元', 'msg-2').json()
    assert second['record_id'] != first['record_id']
    conflict = _say(client, headers, '给房东转30元', 'msg-1')
    assert conflict.json()['code'] == 'IDEMPOTENCY_CONFLICT'


def test_same_message_id_is_scoped_to_owner_and_conversation(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    first = _say(client, headers, '查余额', 'same-message').json()
    second = _say(client, headers, '查余额', 'same-message', conversation='another').json()
    token = client.post('/v1/sessions', json={'user_id': 'user_002'}).json()['access_token']
    third = _say(client, {'Authorization': f'Bearer {token}'}, '查余额', 'same-message').json()
    assert len({first['record_id'], second['record_id'], third['record_id']}) == 3


def _signed(app, client, payload):
    signature = hmac.new(app.state.session_secret.encode(),
                         json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
    return client.post('/v1/im/webim/events', json=payload, headers={'X-IM-Signature': signature})


def test_signed_im_multiturn_keeps_chat_context_and_group_is_separate(tmp_path: Path) -> None:
    app, client, _ = _client(tmp_path)
    base = {'user_id': 'user_001', 'conversation_id': 'im-chat'}
    first = _signed(app, client, base | {'event_id': 'evt-1', 'text': '给房东转账'}).json()
    second = _signed(app, client, base | {'event_id': 'evt-2', 'text': '20元'}).json()
    assert first['kind'] == 'clarification'
    assert second['kind'] == 'confirmation'
    group = _signed(app, client, base | {'event_id': 'evt-3', 'text': '取消', 'group': True}).json()
    assert group['type'] == 'generic_status'
    record = client.get(f"/v1/capability-records/{second['record_id']}", headers={
        'Authorization': 'Bearer ' + client.post('/v1/sessions', json={'user_id': 'user_001'}).json()['access_token']}).json()
    assert record['status'] == 'awaiting_confirmation'


def test_current_user_cards_are_scoped_and_require_auth(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    assert client.get('/v1/cards').status_code == 401
    first = client.get('/v1/cards', headers=headers)
    assert first.status_code == 200
    assert first.json()['cards'][0]['id'] == 'card_001'
    token = client.post('/v1/sessions', json={'user_id': 'user_002'}).json()['access_token']
    second = client.get('/v1/cards', headers={'Authorization': f'Bearer {token}'})
    assert all(item['id'] != 'card_001' for item in second.json()['cards'])


def test_browser_im_requires_binding_and_cannot_supply_bank_identity(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    session = client.post('/v1/im/webim/sessions', json={'identity': 'demo_im_001'})
    assert session.status_code == 200
    im_token = session.json()['access_token']
    im_headers = {'X-IM-Token': im_token}
    message = {'event_id': 'browser-1', 'conversation_id': 'stable', 'text': '给房东转账', 'group': False}
    assert client.post('/v1/im/webim/messages', headers=im_headers, json=message).status_code == 403
    assert client.post('/v1/im/webim/bind', json={'im_token': im_token}).status_code == 401
    assert client.post('/v1/im/webim/bind', headers=headers, json={'im_token': im_token}).status_code == 200
    first = client.post('/v1/im/webim/messages', headers=im_headers, json=message)
    assert first.json()['kind'] == 'clarification'
    retry = client.post('/v1/im/webim/messages', headers=im_headers, json=message)
    assert retry.status_code == 200 and retry.json() == first.json()
    changed = client.post('/v1/im/webim/messages', headers=im_headers, json=message | {'text': '查余额'})
    assert changed.json()['code'] == 'IDEMPOTENCY_CONFLICT'
    second = client.post('/v1/im/webim/messages', headers=im_headers,
                         json=message | {'event_id': 'browser-2', 'text': '20元'})
    assert second.json()['kind'] == 'confirmation'
    forged = client.post('/v1/im/webim/messages', headers=im_headers,
                         json=message | {'event_id': 'browser-3', 'user_id': 'user_002'})
    assert forged.status_code == 422
    assert client.post('/v1/im/webim/messages', json=message).status_code == 401


def test_browser_im_nonce_cannot_be_forwarded_to_another_im_session(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    tokens = [client.post('/v1/im/webim/sessions', json={'identity': 'demo_im_001'}).json()['access_token']
              for _ in range(2)]
    for token in tokens:
        assert client.post('/v1/im/webim/bind', headers=headers, json={'im_token': token}).status_code == 200
    response = client.post('/v1/im/webim/messages', headers={'X-IM-Token': tokens[0]},
                           json={'event_id': 'prepare', 'text': '给房东转20元'}).json()
    forged = client.post('/v1/im/webim/callbacks', headers={'X-IM-Token': tokens[1]},
                        json={'event_id': 'forwarded', 'nonce': response['nonce'], 'action': 'confirm'})
    assert forged.json()['code'] == 'WEBIM_CALLBACK_INVALID'
    success = client.post('/v1/im/webim/callbacks', headers={'X-IM-Token': tokens[0]},
                         json={'event_id': 'valid', 'nonce': response['nonce'], 'action': 'confirm'})
    assert success.json()['data']['transfer_result']['status'] == 'succeeded'


def test_mfa_callback_in_group_does_not_expose_private_record(tmp_path: Path) -> None:
    app, client, _ = _client(tmp_path)
    prepared = _signed(app, client, {'event_id': 'red', 'user_id': 'user_001', 'text': '给房东转1200元'}).json()
    response = _signed(app, client, {'event_id': 'red-callback', 'user_id': 'user_001', 'group': True,
                                    'event_type': 'card_callback', 'nonce': prepared['nonce'], 'action': 'confirm'})
    assert response.json()['type'] == 'generic_status'
    assert 'record_id' not in response.json() and 'app_deep_link' not in response.json()


def test_public_instance_cannot_run_legacy_user_evaluation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('AGENTBANK_ENV', 'public')
    _, client, headers = _client(tmp_path)
    response = client.post('/v1/eval/runs', headers=headers)
    assert response.status_code == 403


def test_staff_free_text_credentials_are_redacted_before_storage() -> None:
    from agentbank.security.dlp import redact_payload, redact_text
    assert '4826' not in redact_text('修改PIN: 4826')
    assert 'hunter2' not in redact_text('password=hunter2')
    assert '1234' not in str(redact_payload({'new_pin': '1234'}))


def test_paid_model_calls_remain_metered_after_downstream_message_failure(tmp_path: Path, monkeypatch) -> None:
    from agentbank.db import LLMUsage
    from agentbank.errors import DomainError
    from agentbank.llm.gateway import LLMGateway, ProviderConfig
    from sqlalchemy import select
    import pytest
    app, _, _ = _client(tmp_path)
    agent = app.state.agent
    config = ProviderConfig('primary', 'https://example.invalid', 'unused', 'model')
    gateway = LLMGateway(app.state.database, config, None, transport=lambda *_: {'ok': True})
    def fail_after_paid_call(*_args):
        gateway.transport(config, 'planner', {})
        raise DomainError('DOWNSTREAM_FAILED')
    monkeypatch.setattr(agent, '_handle_message', fail_after_paid_call)
    for request_id in ['paid-1', 'paid-2']:
        with pytest.raises(DomainError, match='DOWNSTREAM_FAILED'):
            agent.handle('user_001', 'paid', '查余额', message_id=request_id)
    with app.state.database.sessions() as session:
        assert sum(row.calls for row in session.scalars(select(LLMUsage))) == 2
