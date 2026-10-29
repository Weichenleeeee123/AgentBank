from pathlib import Path

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Account, Database, LedgerEntry, MerchantOrder, Operation, Reservation, Transaction
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
        operation = session.query(Operation).filter(Operation.id == f"op_merchant_{stored.id}").one()
        entries = session.query(LedgerEntry).filter(LedgerEntry.operation_id == operation.id).all()
        reservation = session.get(Reservation, reserved.data["reservation_id"])
        account = session.query(Account).filter(Account.user_id == "user_001").one()
        assert sum(entry.amount_minor for entry in entries) == 0
        assert reservation.status == "released" and reservation.amount_minor == 5_000
        assert account.available_minor == 495_000 and account.ledger_balance_minor == 495_000
        assert session.query(Transaction).filter(Transaction.merchant == "鲜花商户").count() == 1


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
    assert service._account("user_001").ledger_balance_minor == 500_000
    with database.sessions() as session:
        assert session.query(Operation).count() == 0
        reservation = session.get(Reservation, reserved.data["reservation_id"])
        assert reservation.status == "released"
