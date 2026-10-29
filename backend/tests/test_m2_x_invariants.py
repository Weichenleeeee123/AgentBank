from pathlib import Path

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database, Mandate, MerchantOrder
from agentbank.seed.loader import load_seed


def make_service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'x.db'}")
    load_seed(database)
    return CapabilityService(database)


def test_x01_uses_trusted_event_and_does_not_spend(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    before = service._account("user_001").available_minor
    result = service.execute("user_001", "X01", {}, "x01-discover")
    assert result.data["recommendation_only"] is True
    assert any(event["event_id"] == "event_birthday" for event in result.data["events"])
    assert service._account("user_001").available_minor == before


def test_x01_surfaces_historical_gift_clues_without_spending(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    result = service.execute("user_001", "X01", {}, "x01-history")
    assert result.data["recommendation_only"] is True
    assert result.data["history_clues"]
    assert {clue["category"] for clue in result.data["history_clues"]} >= {"flower", "cake"}
    assert all(clue["txn_ids"] for clue in result.data["history_clues"])


def test_x02_reserve_then_release_preserves_ledger_balance(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    before = service._account("user_001")
    reserved = service.execute("user_001", "X02", {"amount_minor": 100_000}, "x02-reserve")
    during = service._account("user_001")
    assert during.available_minor == before.available_minor - 100_000
    assert during.ledger_balance_minor == before.ledger_balance_minor
    released = service.release_reservation("user_001", reserved.record_id)
    assert released.data["status"] == "released"
    assert service._account("user_001").available_minor == before.available_minor


def test_x03_x04_orders_are_separate_and_x05_freeze_is_preserved_on_failed_mfa(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    flower = service.execute("user_001", "X03", {"delivery_date": service.clock.business_date(), "amount_minor": 5000}, "x03-order")
    cake = service.execute("user_001", "X04", {"delivery_date": service.clock.business_date(), "amount_minor": 5000}, "x04-order")
    assert flower.data["item"] == "flower" and cake.data["item"] == "cake"
    with service.database.sessions() as session:
        assert session.get(MerchantOrder, flower.data["order_id"]).item != session.get(MerchantOrder, cake.data["order_id"]).item
    frozen = service.execute("user_001", "X05", {"card_id": "card_001"}, "x05-freeze")
    assert frozen.data["freeze_status"] == "frozen"
    assert service.card_payment("user_001", "card_001", 100) == "CARD_RESTRICTED"


def test_x03_order_has_bounded_mandate_and_consumes_it_once(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    order = service.execute("user_001", "X03", {
        "delivery_date": service.clock.business_date(),
        "dispatch_on": service.clock.business_date(),
        "amount_minor": 5_000,
    }, "x03-mandate")
    with service.database.sessions() as session:
        mandate = session.query(Mandate).filter(Mandate.plan_id == "x03-mandate").one()
        assert mandate.operation_type == "merchant.order.create"
        assert mandate.subject_ref == "flower"
        assert mandate.max_amount_minor == 5_000
        assert mandate.max_uses == 1
    progressed = service.dispatch_due("user_001")
    assert any(item.data.get("order_id") == order.data["order_id"] for item in progressed)
    with service.database.sessions() as session:
        mandate = session.query(Mandate).filter(Mandate.plan_id == "x03-mandate").one()
        assert mandate.used_count == 1


def test_x03_dispatch_rejects_changed_amount_after_prepare(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    order = service.execute("user_001", "X03", {"delivery_date": service.clock.business_date(),
                                                 "dispatch_on": service.clock.business_date(),
                                                 "amount_minor": 5_000}, "x03-price")
    with service.database.sessions.begin() as session:
        record = session.get(__import__("agentbank.db", fromlist=["CapabilityRecord"]).CapabilityRecord, order.record_id)
        data = __import__("json").loads(record.data_json)
        data["amount_minor"] = 8_000
        record.data_json = __import__("json").dumps(data)
    progressed = service.dispatch_due("user_001")
    assert progressed[0].data["status"] == "failed"
    assert progressed[0].data["error_code"] == "PRICE_CHANGED"
