from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Account, Database, MerchantOrder, Operation
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


def service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'quotes.db'}")
    load_seed(database)
    return CapabilityService(database, enforce_prepare=True)


def complete(capabilities: CapabilityService, capability_id: str, payload: dict, key: str):
    prepared = capabilities.execute("user_001", capability_id, payload, key)
    return capabilities.confirm_capability("user_001", prepared.record_id, prepared.data["receipt_id"],
                                           prepared.data["action_hash"], prepared.data["display_hash"])


def test_quote_is_persisted_owned_and_price_bound_before_order(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    quote = capabilities.quote_merchant("user_001", "flower", capabilities.clock.business_date())
    assert quote["source"] == "simulated_merchant"
    with pytest.raises(DomainError, match="NOT_OWNER"):
        capabilities.get_merchant_quote("user_002", quote["quote_id"])
    order = complete(capabilities, "X03", {"quote_id": quote["quote_id"],
                                            "delivery_date": quote["delivery_date"],
                                            "amount_minor": quote["amount_minor"]}, "quoted-order")
    capabilities.dispatch_due("user_001")
    with capabilities.database.sessions() as session:
        stored = session.get(MerchantOrder, order.data["order_id"])
        assert stored.quote_id == quote["quote_id"]
        assert stored.status == "delivered"
        assert session.query(Operation).count() == 1


def test_stock_loss_pauses_without_spend_and_retry_requires_available_stock(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    quote = capabilities.quote_merchant("user_001", "cake", capabilities.clock.business_date())
    order = complete(capabilities, "X04", {"quote_id": quote["quote_id"],
                                            "delivery_date": quote["delivery_date"],
                                            "amount_minor": quote["amount_minor"]}, "stock-order")
    capabilities.update_merchant_catalog("cake", stock=0)
    paused = capabilities.dispatch_due("user_001")[0]
    assert paused.status == "paused"
    assert paused.data["error_code"] == "MERCHANT_OUT_OF_STOCK"
    with capabilities.database.sessions() as session:
        assert session.query(Operation).count() == 0
        assert session.query(Account).filter(Account.user_id == "user_001").one().available_minor == 500_000
    with pytest.raises(DomainError, match="MERCHANT_OUT_OF_STOCK"):
        capabilities.retry_merchant_order("user_001", order.record_id)
    capabilities.update_merchant_catalog("cake", stock=2)
    capabilities.retry_merchant_order("user_001", order.record_id)
    assert capabilities.dispatch_due("user_001")[0].data["status"] == "delivered"


def test_catalog_price_change_requires_reconfirmation_and_renews_quote(tmp_path: Path) -> None:
    capabilities = service(tmp_path)
    quote = capabilities.quote_merchant("user_001", "flower", capabilities.clock.business_date())
    order = complete(capabilities, "X03", {"quote_id": quote["quote_id"],
                                            "delivery_date": quote["delivery_date"],
                                            "amount_minor": quote["amount_minor"]}, "price-quote-order")
    capabilities.update_merchant_catalog("flower", price_minor=8000)
    paused = capabilities.dispatch_due("user_001")[0]
    assert paused.status == "paused" and paused.data["error_code"] == "PRICE_CHANGED"
    refreshed = capabilities.reconfirm_merchant_price("user_001", order.record_id, 8000)
    assert refreshed.status == "awaiting_confirmation"
    assert refreshed.data["payload"]["quote_id"] != quote["quote_id"]
    accepted = capabilities.confirm_capability("user_001", refreshed.record_id, refreshed.data["receipt_id"],
                                               refreshed.data["action_hash"], refreshed.data["display_hash"])
    assert accepted.status == "scheduled"
    assert capabilities.dispatch_due("user_001")[0].data["status"] == "delivered"
