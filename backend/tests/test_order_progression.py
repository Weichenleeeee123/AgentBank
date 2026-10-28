from pathlib import Path

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database, MerchantOrder
from agentbank.seed.loader import load_seed


def test_merchant_order_progression_charges_reservation_and_delivers(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'orders.db'}")
    load_seed(database)
    service = CapabilityService(database)
    reserved = service.execute("user_001", "X02", {"amount_minor": 10_000}, "order-reserve")
    order = service.execute("user_001", "X03", {"delivery_date": service.clock.business_date(),
                                                 "amount_minor": 5_000, "reservation_id": reserved.data["reservation_id"]}, "order-create")
    progressed = service.dispatch_due("user_001")
    assert any(item.data.get("order_id") == order.data["order_id"] for item in progressed)
    with database.sessions() as session:
        stored = session.get(MerchantOrder, order.data["order_id"])
        assert stored is not None
        assert stored.status == "delivered"


def test_merchant_order_failure_releases_reservation(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'orders-fail.db'}")
    load_seed(database)
    service = CapabilityService(database)
    before = service._account("user_001").available_minor
    reserved = service.execute("user_001", "X02", {"amount_minor": 10_000}, "order-fail-reserve")
    service.execute("user_001", "X03", {"delivery_date": service.clock.business_date(),
                                         "amount_minor": 5_000, "reservation_id": reserved.data["reservation_id"],
                                         "simulate_failure": True}, "order-fail-create")
    service.dispatch_due("user_001")
    assert service._account("user_001").available_minor == before
