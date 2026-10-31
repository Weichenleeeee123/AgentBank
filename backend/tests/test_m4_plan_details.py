from fastapi.testclient import TestClient

from agentbank.api.app import create_app


def _auth(client, user_id):
    token = client.post('/v1/sessions', json={'user_id': user_id}).json()['access_token']
    return {'Authorization': f'Bearer {token}'}


def test_plan_deep_link_details_restore_owned_transfer_confirmation(tmp_path):
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'plan-details.db'}"))
    owner = _auth(client, 'user_001')
    prepared = client.post('/v1/transfers/prepare', headers=owner,
                           json={'recipient_name': '房东', 'amount_minor': 2000, 'memo': '房租'}).json()
    path = f"/v1/plans/{prepared['plan_id']}"
    restored = client.get(path, headers=owner)
    assert restored.status_code == 200
    data = restored.json()
    for field in ['display', 'display_hash', 'action_hash', 'receipt_id', 'assurance']:
        assert data[field] == prepared[field]
    assert client.get(path).status_code == 401
    foreign = client.get(path, headers=_auth(client, 'user_002'))
    assert foreign.json()['code'] == 'NOT_OWNER'
    assert 'display' not in foreign.json() and 'receipt_id' not in foreign.json()
    confirmed = client.post(path + '/confirm', headers=owner,
                            json={'receipt_id': data['receipt_id'], 'display_hash': data['display_hash']})
    assert confirmed.json()['status'] == 'succeeded'
    assert client.get(path, headers=owner).json()['receipt_id'] is None


def test_staff_workbench_routes_work_with_observed_metrics_and_safe_drafts(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTBANK_STAFF_TOKEN', 'independent-staff-token')
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'staff-routes.db'}"))
    owner = _auth(client, 'user_001')
    case = client.post('/v1/handoffs', headers=owner,
                       json={'operation_type': 'transfer', 'reason': '人工核实'}).json()['case_id']
    headers = {'X-Staff-Token': 'independent-staff-token'}
    assert client.get('/staff/v1/metrics', headers=owner).status_code == 403
    metrics = client.get('/staff/v1/metrics', headers=headers)
    assert metrics.status_code == 200 and metrics.json()['requests']['count'] > 0
    base = f'/staff/v1/handoffs/{case}'
    assert client.post(base + '/draft', headers=headers,
                       json={'note': '已核实', 'user_message': '请重新核对并授权'}).status_code == 200
    assert client.post(base + '/retry-request', headers=headers,
                       json={'note': '交还用户'}).json()['status'] == 'awaiting_user'
    listed = client.get('/staff/v1/handoffs', headers=headers).json()['cases']
    assert any(row['id'] == case for row in listed)
    notices = client.get('/v1/notifications', headers=owner).json()['notifications']
    assert any(row['event_type'] == 'handoff.retry_requested' for row in notices)
