from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database, InvestmentOrder, Position
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
    positions = service.execute("user_001", "W06", {}, "w06-positions")
    assert positions.data["source"] == "simulated_bank"
    assert positions.data["as_of"] and positions.data["positions"]


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
