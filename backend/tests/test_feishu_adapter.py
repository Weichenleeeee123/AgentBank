import hashlib
import hmac
import json
from pathlib import Path

import pytest

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.channels.feishu import FeishuAdapter
from agentbank.db import Database
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


def test_feishu_adapter_routes_signed_event_and_rejects_replay(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'feishu.db'}")
    load_seed(database)
    adapter = FeishuAdapter("secret")
    payload = {"event_id": "feishu-1", "user_id": "user_001", "text": "查余额"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    signature = hmac.new(b"secret", body.encode(), hashlib.sha256).hexdigest()
    response = adapter.handle(payload, signature, AgentOrchestrator(database))
    assert response["type"] == "facts"
    with pytest.raises(DomainError, match="WEBIM_REPLAYED"):
        adapter.handle(payload, signature, AgentOrchestrator(database))
