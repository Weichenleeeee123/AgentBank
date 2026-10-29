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
    risk = agent.handle("user_001", "risk", "1 1 1 1 1 1 1 1 1 1")
    assert risk.kind == "confirmation"
    agent.confirm("user_001", risk.record_id, risk.display_hash, risk.data["receipt_id"])
    response = agent.handle("user_001", "invest", "申购 R1 20 元")
    assert response.kind == "confirmation"
    response = agent.confirm("user_001", response.record_id, response.display_hash,
                             response.data["receipt_id"])
    assert response.kind == "mfa"
    challenge = device.inbox(token)[0]
    completed = agent.verify("user_001", response.record_id, challenge["code"])
    assert completed.data["status"] == "subscribed"


def test_agent_x05_confirm_then_device_verification_completes_loss_report(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'x05-agent.db'}")
    load_seed(database)
    device = DeviceService(database)
    token = device.register("user_001")
    agent = AgentOrchestrator(database)
    agent.capabilities.device_tokens["user_001"] = token

    response = agent.handle("user_001", "loss", "我的卡被盗刷了")
    assert response.kind == "confirmation"
    response = agent.confirm("user_001", response.record_id, response.display_hash,
                             response.data["receipt_id"])
    assert response.kind == "mfa"
    challenge = device.inbox(token)[0]
    completed = agent.verify("user_001", response.record_id, challenge["code"])
    assert completed.data["loss_report_status"] == "succeeded"
    assert completed.data["freeze_status"] == "lost"
