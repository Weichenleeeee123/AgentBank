from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database, InvestmentOrder, LedgerEntry, Operation, Position
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


def make_service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'w.db'}")
    load_seed(database)
    return CapabilityService(database)


def test_w01_w02_w03_risk_and_product_contracts(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(DomainError, match="RISK_PROFILE_REQUIRED"):
        service.execute("user_001", "W01", {}, "w01-no-profile")
    profile = service.execute("user_001", "W03", {"answers": [1] * 10}, "w03-profile")
    assert profile.data["level"] == "C1"
    recommended = service.execute("user_001", "W01", {}, "w01-recommend")
    assert recommended.data["products"]
    assert all(item["risk_level"] <= "R1" for item in recommended.data["products"])
    compared = service.execute("user_001", "W02", {}, "w02-compare")
    assert all({"risk_level", "term_days", "fee_bps", "redemption_days"} <= set(item) for item in compared.data["products"])


def test_w04_suitability_and_w06_position_evidence(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.execute("user_001", "W03", {"answers": [1] * 10}, "w04-risk")
    with pytest.raises(DomainError, match="SUITABILITY_MISMATCH"):
        service.execute("user_001", "W04", {"product_id": "product_R3", "amount_minor": 1_000, "assurance": "mfa"}, "w04-deny")
    before = service._account("user_001").available_minor
    subscribed = service.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa"}, "w04-ok")
    assert service._account("user_001").available_minor == before - 1_000
    with service.database.sessions() as session:
        assert session.get(InvestmentOrder, subscribed.data["order_id"]).status == "subscribed"
        assert session.query(Position).filter(Position.user_id == "user_001").count() == 1
        operation = session.get(Operation, subscribed.data["operation_id"])
        entries = session.query(LedgerEntry).filter(LedgerEntry.operation_id == operation.id).all()
        assert len(entries) == 2 and sum(entry.amount_minor for entry in entries) == 0
    ledger_balance = service._account("user_001").ledger_balance_minor
    with service.database.sessions() as session:
        ledger_entries_before = session.query(LedgerEntry).count()
    positions = service.execute("user_001", "W06", {}, "w06-positions")
    initial_value = positions.data["positions"][0]["market_value_minor"]
    service.clock.advance(days=10)
    later = service.execute("user_001", "W06", {}, "w06-positions-later")
    assert positions.data["source"] == "simulated_market"
    assert positions.data["as_of"] and positions.data["positions"]
    assert positions.data["positions"][0]["order_ids"]
    assert later.data["positions"][0]["market_value_minor"] != initial_value
    assert service._account("user_001").ledger_balance_minor == ledger_balance
    with service.database.sessions() as session:
        assert session.query(LedgerEntry).count() == ledger_entries_before


def test_w05_processing_then_settled_restores_cash(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.execute("user_001", "W03", {"answers": [2] * 10}, "w05-risk")
    before = service._account("user_001").available_minor
    subscribed = service.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 2_000, "assurance": "mfa"}, "w05-subscribe")
    requested = service.execute("user_001", "W05", {"order_id": subscribed.data["order_id"], "assurance": "mfa"}, "w05-redeem")
    assert requested.status == "processing"
    settled = service.progress_investments("user_001")
    assert settled and settled[0].data["status"] == "settled"
    assert service._account("user_001").available_minor == before
    with service.database.sessions() as session:
        operation = session.get(Operation, settled[0].data["operation_id"])
        entries = session.query(LedgerEntry).filter(LedgerEntry.operation_id == operation.id).all()
        assert len(entries) == 2 and sum(entry.amount_minor for entry in entries) == 0
    positions = service.execute("user_001", "W06", {}, "w05-empty-positions")
    assert positions.data["positions"][0]["market_value_minor"] == 0


def test_w05_rejects_non_subscribed_order_and_insufficient_position(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.execute("user_001", "W03", {"answers": [2] * 10}, "w05-precondition-risk")
    with pytest.raises(DomainError, match="ORDER_NOT_REDEEMABLE"):
        service.execute("user_001", "W05", {"order_id": "missing", "assurance": "mfa"}, "w05-missing")
    subscribed = service.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa"}, "w05-precondition-subscribe")
    with service.database.sessions.begin() as session:
        position = session.query(Position).filter(Position.user_id == "user_001", Position.product_id == "product_R1").one()
        position.shares = 0
    with pytest.raises(DomainError, match="INSUFFICIENT_POSITION"):
        service.execute("user_001", "W05", {"order_id": subscribed.data["order_id"], "assurance": "mfa"}, "w05-no-position")
