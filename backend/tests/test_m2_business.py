from datetime import date
from pathlib import Path

import pytest

from agentbank.bank.business import (
    BirthdayPlanner,
    CardService,
    MandateService,
    RecipientResolver,
    RiskService,
    SubscriptionService,
)
from agentbank.clock import BusinessClock
from agentbank.db import Database
from agentbank.errors import AmbiguousRecipient, DomainError
from agentbank.seed.loader import load_seed
from agentbank.tools import TOOL_REGISTRY


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'business.db'}")
    load_seed(database)
    return database


def test_recipient_resolver_requires_disambiguation_and_supports_phone(database: Database) -> None:
    resolver = RecipientResolver(database)
    with pytest.raises(AmbiguousRecipient):
        resolver.by_name("user_001", "张三")
    candidate = resolver.by_phone("user_001", "13800000002")
    assert candidate.masked_name == "*三"
    assert candidate.is_new is True


def test_risk_questionnaire_is_deterministic_and_filters_products(database: Database) -> None:
    service = RiskService(database)
    profile = service.submit("user_001", [2, 2, 1, 1, 1, 1, 1, 1, 1, 1])
    products = service.recommend("user_001")
    assert profile.level == "C2"
    assert all(item.risk_level <= "R2" for item in products)


def test_expired_risk_profile_cannot_recommend_or_subscribe(database: Database) -> None:
    service = RiskService(database)
    service.submit("user_001", [1] * 10)
    from agentbank.db import RiskProfile
    with database.sessions.begin() as session:
        profile = session.get(RiskProfile, "user_001")
        assert profile is not None
        profile.expires_on = "2020-01-01"
    with pytest.raises(DomainError, match="RISK_PROFILE_EXPIRED"):
        service.recommend("user_001")


def test_card_protection_state_transitions(database: Database) -> None:
    cards = CardService(database)
    card_id = cards.freeze("user_001")
    assert cards.status(card_id) == "frozen"
    with pytest.raises(DomainError, match="MFA_REQUIRED"):
        cards.unfreeze("user_001", card_id, assurance="confirm")
    cards.unfreeze("user_001", card_id, assurance="mfa")
    assert cards.status(card_id) == "active"


def test_subscription_cancel_revokes_future_charge(database: Database) -> None:
    service = SubscriptionService(database)
    subscription = service.list("user_001")[0]
    service.cancel("user_001", subscription.id)
    assert service.get(subscription.id).status == "cancelled"
    assert service.should_charge(subscription.id, date(2026, 12, 1)) is False


def test_mandate_is_bounded_and_business_clock_drives_due_orders(database: Database) -> None:
    clock = BusinessClock()
    mandates = MandateService(database, clock)
    mandate = mandates.create("user_001", "birthday-plan", "merchant.order.create", "flower", 20_000, 1, date(2026, 11, 1), date(2026, 11, 30))
    clock.advance_days(4)
    assert mandates.consume(mandate.id, 10_000) is True
    assert mandates.consume(mandate.id, 10_000) is False
    planner = BirthdayPlanner(database, clock, mandates)
    assert planner.discover("user_001")
    reservation_id = planner.reserve("user_001", 5_000, "birthday-plan")
    planner.release("user_001", reservation_id)


def test_tool_registry_covers_all_declared_capabilities() -> None:
    for capability in ("Q01", "T01", "B01", "W01", "C01", "S01", "X05"):
        assert capability in TOOL_REGISTRY
