from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi.testclient import TestClient
from sqlalchemy import select

from agentbank.agent.orchestrator import AgentResponse
from agentbank.api.app import create_app
from agentbank.db import ImDelivery, LedgerEntry, WebImEvent


def _bound_client(app):
    client = TestClient(app, raise_server_exceptions=False)
    token = client.post('/v1/sessions', json={'user_id': 'user_001'}).json()['access_token']
    im_token = client.post('/v1/im/webim/sessions', json={'identity': 'demo_im_001'}).json()['access_token']
    response = client.post('/v1/im/webim/bind', headers={'Authorization': f'Bearer {token}'},
                           json={'im_token': im_token})
    assert response.status_code == 200
    return client, {'X-IM-Token': im_token}


def test_busy_im_event_replays_original_failure_even_after_restart(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'im-busy.db'}"
    app = create_app(url)
    client, headers = _bound_client(app)
    first_entered, finish_first = Event(), Event()
    calls = []

    def work(*args):
        calls.append(args[2])
        if args[2] == 'first':
            first_entered.set()
            assert finish_first.wait(5)
        return AgentResponse(kind='result', intent='safe')

    monkeypatch.setattr(app.state.agent, '_handle_message', work)
    busy = {'event_id': 'busy', 'text': 'second', 'conversation_id': 'same'}
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(client.post, '/v1/im/webim/messages', headers=headers,
                              json={'event_id': 'first', 'text': 'first', 'conversation_id': 'same'})
        try:
            assert first_entered.wait(5)
            original = client.post('/v1/im/webim/messages', headers=headers, json=busy)
            assert original.json()['code'] == 'CONVERSATION_BUSY'
        finally:
            finish_first.set()
        assert running.result().status_code == 200
    retry = client.post('/v1/im/webim/messages', headers=headers, json=busy)
    assert retry.status_code == original.status_code
    assert retry.json() == original.json()
    assert '新消息' in retry.json()['explanation']['next_step']
    assert calls == ['first']
    with app.state.database.sessions() as session:
        assert len(session.scalars(select(WebImEvent)).all()) == 2
        assert not session.scalars(select(LedgerEntry)).all()
        assert all(row.response_json for row in session.scalars(select(ImDelivery)))
    restarted = TestClient(create_app(url))
    restored = restarted.post('/v1/im/webim/messages', headers=headers, json=busy)
    assert restored.status_code == original.status_code and restored.json() == original.json()
    assert client.post('/v1/im/webim/messages', headers=headers,
                       json=busy | {'event_id': 'new'}).status_code == 200
    assert calls == ['first', 'second']


def test_unexpected_im_processing_failure_does_not_redispatch(tmp_path, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path / 'im-unknown.db'}")
    client, headers = _bound_client(app)
    calls = []

    def fail(*args):
        calls.append(1)
        raise RuntimeError('private internal detail')

    monkeypatch.setattr(app.state.agent, '_handle_message', fail)
    body = {'event_id': 'unknown', 'text': 'request'}
    assert client.post('/v1/im/webim/messages', headers=headers, json=body).status_code == 500
    retry = client.post('/v1/im/webim/messages', headers=headers, json=body)
    assert retry.json()['code'] == 'MESSAGE_OUTCOME_UNKNOWN'
    assert 'private internal detail' not in retry.text
    assert len(calls) == 1
