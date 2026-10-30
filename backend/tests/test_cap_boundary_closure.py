from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database, Transaction
from agentbank.errors import AmbiguousRecipient, DomainError
from agentbank.seed.loader import load_seed


def service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'cap-boundary.db'}")
    load_seed(database)
    return CapabilityService(database)


def test_q01_returns_only_the_authenticated_subject_balance(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    own = capabilities.execute("user_001", "Q01", {}, "q01-boundary-own")
    other = capabilities.execute("user_002", "Q01", {}, "q01-boundary-other")
    assert own.data["available_minor"] == capabilities._account("user_001").available_minor
    assert other.data["available_minor"] == capabilities._account("user_002").available_minor
    assert own.data["account_id"] != other.data["account_id"]


def test_q02_end_date_is_inclusive_for_datetime_rows_and_stays_user_scoped(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    with capabilities.database.sessions.begin() as session:
        session.add(Transaction(id="q02-end-boundary", user_id="user_001", posted_at="2026-01-31T23:59:59.999999",
                                amount_minor=100, merchant="same-day", category="test", direction="debit"))
        session.add(Transaction(id="q02-other-user", user_id="user_002", posted_at="2026-01-31T12:00:00",
                                amount_minor=100, merchant="same-day", category="test", direction="debit"))
    result = capabilities.execute("user_001", "Q02", {"start_date": "2026-01-31", "end_date": "2026-01-31"},
                                  "q02-boundary")
    ids = {item["txn_id"] for item in result.data["transactions"]}
    assert "q02-end-boundary" in ids
    assert "q02-other-user" not in ids


def test_t02_unknown_phone_and_t03_ambiguous_alias_never_prepare(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    with pytest.raises(DomainError, match="RECIPIENT_NOT_FOUND"):
        capabilities.execute("user_001", "T02", {"phone": "13900000000", "amount_minor": 1000}, "t02-boundary")
    with pytest.raises(AmbiguousRecipient):
        capabilities.execute("user_001", "T03", {"alias": "张三", "amount_minor": 1000}, "t03-boundary")


def test_t02_unknown_phone_is_denied_without_creating_capability_record(tmp_path: Path) -> None:
    from agentbank.db import CapabilityRecord

    capabilities = service(tmp_path)
    with pytest.raises(DomainError, match="RECIPIENT_NOT_FOUND"):
        capabilities.execute("user_001", "T02", {"phone": "13900000000", "amount_minor": 1000},
                             "t02-unknown-phone")
    with capabilities.database.sessions() as session:
        assert session.query(CapabilityRecord).count() == 0


def test_t05_duplicate_participants_are_rejected_before_bill_creation(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    with pytest.raises(DomainError):
        capabilities.execute("user_001", "T05", {"total_minor": 10000,
                                                   "participant_ids": ["user_001", "user_002", "user_002"]},
                              "t05-duplicate-participant")
    from agentbank.db import AABill
    with capabilities.database.sessions() as session:
        assert session.query(AABill).count() == 0


def test_w01_missing_profile_and_w02_unknown_product_are_denied(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    with pytest.raises(DomainError, match="RISK_PROFILE_REQUIRED"):
        capabilities.execute("user_001", "W01", {}, "w01-boundary-no-profile")
    result = capabilities.execute("user_001", "W02", {"product_ids": ["not-registered"]}, "w02-boundary-unknown")
    assert result.data["products"] == []


def test_w04_unsuitable_product_is_denied_without_debit(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    capabilities.execute("user_001", "W03", {"answers": [0] * 10}, "w04-boundary-risk")
    balance = capabilities._account("user_001").available_minor
    with pytest.raises(DomainError, match="SUITABILITY_MISMATCH"):
        capabilities.execute("user_001", "W04", {"product_id": "product_R3", "amount_minor": 1000, "assurance": "mfa"},
                              "w04-boundary-suitability")
    assert capabilities._account("user_001").available_minor == balance


def test_w06_positions_are_scoped_to_the_requesting_user(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    capabilities.execute("user_001", "W03", {"answers": [2] * 10}, "w06-boundary-risk")
    capabilities.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 1000, "assurance": "mfa"},
                         "w06-boundary-position")
    other = capabilities.execute("user_002", "W06", {}, "w06-boundary-other")
    assert other.data["positions"] == []


def test_c02_limit_above_product_ceiling_is_denied(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    with pytest.raises(DomainError, match="CARD_LIMIT_EXCEEDED"):
        capabilities.execute("user_001", "C02", {"card_id": "card_001", "limit_minor": 500001,
                                                   "assurance": "mfa"}, "c02-boundary-limit")


def test_s01_evidence_is_user_scoped_and_distinguishes_confirmed_suspected(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    with capabilities.database.sessions.begin() as session:
        session.add_all([
            Transaction(id=f"s01-other-{index}", user_id="user_002", posted_at=f"2026-0{index + 1}-01",
                        amount_minor=2990, merchant="视频会员", category="订阅", direction="debit")
            for index in range(1, 4)
        ])
    result = capabilities.execute("user_001", "S01", {}, "s01-boundary")
    subscriptions = {item["merchant"]: item for item in result.data["subscriptions"]}
    assert subscriptions["视频会员"]["status"] == "confirmed"
    assert subscriptions["AI 工具"]["status"] == "suspected"
    assert all(not txn_id.startswith("s01-other-") for item in subscriptions.values()
               for txn_id in item["evidence_txn_ids"])
