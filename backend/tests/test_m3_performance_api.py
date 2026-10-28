from pathlib import Path

from fastapi.testclient import TestClient

from agentbank.api.app import create_app
from agentbank.evals.api_performance import benchmark_callable


def test_g_query_and_conversation_performance_are_measured(tmp_path: Path) -> None:
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'perf-api.db'}"))
    token = client.post("/v1/sessions", json={"user_id": "user_001"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    g_result = benchmark_callable(lambda: client.get("/v1/accounts/balance", headers=headers))
    agent_result = benchmark_callable(lambda: client.post("/v1/conversations/perf/messages", headers=headers,
                                                          json={"text": "帮我查一下余额"}))
    assert g_result.p95_ms >= g_result.p50_ms
    assert agent_result.p95_ms >= agent_result.p50_ms
