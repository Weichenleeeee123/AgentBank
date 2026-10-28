import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.analysis.dsl import DslExecutor
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.security.breaker import CircuitBreaker
from agentbank.api.app import create_app


def make_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'm2-gate.db'}")
    load_seed(database)
    return database


def test_m2_dsl_is_read_only_user_scoped_and_bounded(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    result = DslExecutor(database).run("user_001", {"source": "transactions", "group_by": ["month"], "limit": 100})
    assert result.rows and all(row["user_id"] == "user_001" for row in result.rows)
    with pytest.raises(ValueError, match="DSL_FIELD_NOT_ALLOWED"):
        DslExecutor(database).run("user_001", {"source": "transactions", "filters": [{"field": "user_id", "op": "eq", "value": "user_002"}]})
    with pytest.raises(ValueError, match="DSL_METRIC_NOT_ALLOWED"):
        DslExecutor(database).run("user_001", {"source": "transactions", "metrics": [{"fn": "exec"}]})


def test_m2_webim_routes_agent_and_rejects_replay(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path / 'webim-gate.db'}")
    client = TestClient(app)
    event = {"event_id": "m2-webim-1", "user_id": "user_001", "text": "帮我查一下余额"}
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    signature = hmac.new(app.state.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    response = client.post("/v1/im/webim/events", json=event, headers={"X-IM-Signature": signature})
    assert response.status_code == 200 and response.json()["type"] == "facts"
    replay = client.post("/v1/im/webim/events", json=event, headers={"X-IM-Signature": signature})
    assert replay.status_code == 409


def test_m2_protective_operations_survive_breaker_lock(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    breaker = CircuitBreaker(database)
    for _ in range(3):
        breaker.record_block("user_001", "injection")
    service = CapabilityService(database)
    with pytest.raises(DomainError, match="SECURITY_LOCKED"):
        service.execute("user_001", "S02", {"subscription_id": "sub_video"}, "m2-lock-reminder")
    frozen = service.execute("user_001", "C07", {"card_id": "card_001"}, "m2-lock-freeze")
    assert frozen.data["payment_blocked"] is True


def test_m2_agent_proactive_birthday_discovery_is_recommendation_only(tmp_path: Path) -> None:
    response = AgentOrchestrator(make_database(tmp_path)).handle("user_001", "m2-birthday", "我爱人生日快到了")
    assert response.capability_id == "X01"
    assert response.data["recommendation_only"] is True


def test_m2_plan_graph_is_visible_and_mandate_can_be_revoked(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = CapabilityService(database)
    scheduled = service.execute("user_001", "T04", {"recipient_name": "房东", "amount_minor": 1_000, "max_uses": 1}, "m2-mandate")
    service.revoke_mandate("user_001", scheduled.data["mandate_id"])
    paused = service.dispatch_due("user_001")
    assert paused[0].status == "paused"
    from agentbank.workflow.planner import BusinessPlanService
    graph = BusinessPlanService(database).create_birthday_plan("user_001", "event_birthday", 100_000)
    visible = BusinessPlanService(database).graph(graph.plan_id, "user_001")
    assert len(visible["nodes"]) == 4 and all("authorization" in node for node in visible["nodes"])
