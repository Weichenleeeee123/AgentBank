from pathlib import Path

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.auth.devices import DeviceService
from agentbank.db import Database
from agentbank.seed.loader import load_seed


def test_agent_red_investment_creates_challenge_then_completes(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'red-agent.db'}")
    load_seed(database)
    device = DeviceService(database)
    token = device.register("user_001")
    agent = AgentOrchestrator(database)
    agent.capabilities.device_tokens["user_001"] = token
    agent.handle("user_001", "risk", "做风险测评")
    agent.handle("user_001", "risk", "1 1 1 1 1 1 1 1 1 1")
    response = agent.handle("user_001", "invest", "申购 R1 20 元")
    assert response.kind == "mfa"
    challenge = device.inbox(token)[0]
    completed = agent.verify("user_001", response.record_id, challenge["code"])
    assert completed.data["status"] == "subscribed"
