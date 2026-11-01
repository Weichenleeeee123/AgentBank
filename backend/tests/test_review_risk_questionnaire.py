from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from agentbank.api.app import create_app
from agentbank.bank.business import RiskService
from agentbank.clock import BusinessClock
from agentbank.db import Database
from agentbank.seed.loader import load_seed


def test_risk_conversation_provides_versioned_answerable_questionnaire(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTBANK_LLM_API_KEY', raising=False)
    app = create_app(f"sqlite:///{tmp_path / 'risk.db'}")
    client = TestClient(app)
    h = {'Authorization': 'Bearer ' + client.post('/v1/sessions', json={'user_id': 'user_001'}).json()['access_token']}
    response = client.post('/v1/conversations/risk/messages', headers=h, json={'text': '我要做风险测评'}).json()
    questionnaire = response['data']['questionnaire']
    assert len(questionnaire['questions']) == 10
    assert all(len(q['options']) == 6 and q['text'] for q in questionnaire['questions'])
    assert client.get('/v1/risk/questionnaire').status_code == 401
    assert client.get('/v1/risk/questionnaire', headers=h).json() == questionnaire
    prepared = client.post('/v1/risk/submit', headers=h, json={'answers': [1] * 10,
        'questionnaire_version': questionnaire['version']}).json()
    assert prepared['status'] == 'awaiting_confirmation'
    assert len(prepared['data']['display']) >= 14
    result = client.post('/v1/capability-records/' + prepared['record_id'] + '/confirm', headers=h,
        json={'receipt_id': prepared['data']['receipt_id'], 'display_hash': prepared['data']['display_hash']}).json()
    assert result['data']['questionnaire_version'] == questionnaire['version']
    assert result['data']['answers'] == [1] * 10
    assert client.post('/v1/risk/submit', headers=h, json={'answers': [True] * 10}).status_code >= 400


def test_risk_validity_is_twelve_calendar_months_including_leap_day():
    db = Database('sqlite://')
    load_seed(db)
    clock = BusinessClock(datetime(2028, 2, 29, 8, tzinfo=timezone.utc))
    profile = RiskService(db, clock=clock).submit('user_001', [1] * 10)
    assert profile.expires_on == '2029-02-28'
    from agentbank.db import RiskAssessment
    with db.sessions() as session:
        evidence = session.scalar(select(RiskAssessment).where(RiskAssessment.user_id == 'user_001'))
        assert evidence.assessed_on == '2028-02-29'
        assert evidence.questionnaire_version
