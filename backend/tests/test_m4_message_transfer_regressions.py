from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json

import pytest
from sqlalchemy import select

from agentbank.agent.orchestrator import AgentOrchestrator, AgentResponse
from agentbank.bank.service import TransferService
from agentbank.db import AgentMessage, Database, LedgerEntry, Payee, PlanRecord, ReceiptRecord
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed
from agentbank.security.explain import explain_denial


def test_distinct_concurrent_messages_do_not_interleave_conversation_state(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'concurrent.db'}")
    load_seed(db)
    first_entered, finish_first = Event(), Event()
    first = AgentOrchestrator(db)
    second = AgentOrchestrator(db)

    def first_work(*args):
        first_entered.set()
        assert finish_first.wait(5)
        return AgentResponse(kind="result", intent="first")

    first._handle_message = first_work
    second._handle_message = lambda *args: AgentResponse(kind="result", intent="second")
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.handle, "user_001", "same-chat", "first", message_id="first")
        try:
            assert first_entered.wait(5)
            with pytest.raises(DomainError, match="CONVERSATION_BUSY"):
                second.handle("user_001", "same-chat", "second", message_id="second")
            with db.sessions() as session:
                assert session.get(AgentMessage, ("user_001", "same-chat", "second")) is None
            # An independent conversation must remain usable while the first waits on a model.
            assert second.handle("user_001", "other-chat", "other", message_id="other").intent == "second"
        finally:
            finish_first.set()
        assert running.result().intent == "first"
    assert second.handle("user_001", "same-chat", "second", message_id="second").intent == "second"


def test_failed_message_retry_never_reissues_work_and_releases_conversation(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'failure.db'}")
    load_seed(db)
    agent = AgentOrchestrator(db)
    attempts = []

    def fail(*args):
        attempts.append(1)
        raise RuntimeError("private provider detail")

    agent._handle_message = fail
    with pytest.raises(RuntimeError):
        agent.handle("user_001", "same", "request", message_id="failed")
    with pytest.raises(DomainError, match="MESSAGE_PROCESSING_FAILED"):
        agent.handle("user_001", "same", "request", message_id="failed")
    assert len(attempts) == 1
    agent._handle_message = lambda *args: AgentResponse(kind="result", intent="new")
    assert agent.handle("user_001", "same", "new", message_id="new").intent == "new"
    explanation = explain_denial("MESSAGE_PROCESSING_FAILED")
    assert "查询" in explanation["next_step"]


def test_transfer_confirmation_persists_complete_exact_display_and_target_snapshot():
    db = Database("sqlite://")
    load_seed(db)
    transfer = TransferService(db)
    amount = 9007199254740993
    prepared = transfer.prepare("user_001", "房东", amount, "十月房租")
    labels = {item["label"]: item["value"] for item in prepared.display}
    assert labels["金额"] == "¥90071992547409.93"
    assert labels["备注"] == "十月房租"
    assert labels["付款账户"] and labels["收款账户"]
    with db.sessions() as session:
        plan = session.get(PlanRecord, prepared.plan_id)
        assert json.loads(plan.display_json) == prepared.display


def test_changed_payee_destination_cannot_execute_old_confirmation():
    db = Database("sqlite://")
    load_seed(db)
    transfer = TransferService(db)
    prepared = transfer.prepare("user_001", "房东", 2000)
    with db.sessions.begin() as session:
        payee = session.scalar(select(Payee).where(Payee.owner_user_id == "user_001",
                                                    Payee.display_name == "房东"))
        payee.account_id = "different_destination"
    with pytest.raises(DomainError, match="ACTION_CHANGED"):
        transfer.confirm("user_001", prepared.plan_id, prepared.receipt_id, prepared.display_hash)
    with db.sessions() as session:
        assert not session.scalars(select(LedgerEntry)).all()
        assert session.get(ReceiptRecord, prepared.receipt_id).status == "pending"
