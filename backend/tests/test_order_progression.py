from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Account, AuditLog, CapabilityRecord, Database, LedgerEntry, Mandate, MerchantOrder, Operation, Reservation, Transaction
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


@pytest.mark.parametrize("capability_id", ["X03", "X04"])
def test_merchant_order_failure_releases_reservation(tmp_path: Path, capability_id: str) -> None:
    database = Database(f"sqlite:///{tmp_path / 'orders-fail.db'}")
    load_seed(database)
    service = CapabilityService(database)
    before = service._account("user_001").available_minor
    reserved = service.execute("user_001", "X02", {"amount_minor": 10_000}, "order-fail-reserve")
    payload = {"delivery_date": service.clock.business_date(), "amount_minor": 5_000,
               "reservation_id": reserved.data["reservation_id"], "simulate_failure": True}
    failed = service.execute("user_001", capability_id, payload, "order-fail-create")
    assert failed.status == "failed"
    assert service._account("user_001").available_minor == before
    assert service._account("user_001").ledger_balance_minor == 500_000
    with database.sessions() as session:
        stored = session.get(MerchantOrder, failed.data["order_id"])
        operation = session.get(Operation, f"op_merchant_{stored.id}")
        record = session.get(CapabilityRecord, failed.record_id)
        assert stored.status == "failed"
        assert operation.status == "failed" and operation.error_code == "MERCHANT_UNAVAILABLE"
        assert record.status == "failed"
        assert session.query(AuditLog).filter(AuditLog.event_type == "merchant.order.compensated").count() == 1
        reservation = session.get(Reservation, reserved.data["reservation_id"])
        assert reservation.status == "released"
    assert service.dispatch_due("user_001") == []
    assert service.execute("user_001", capability_id, payload, "order-fail-create").record_id == failed.record_id
    assert service._account("user_001").available_minor == before
    with database.sessions() as session:
        assert session.query(Operation).filter(Operation.id == f"op_merchant_{failed.data['order_id']}").count() == 1
        assert session.query(AuditLog).filter(AuditLog.event_type == "merchant.order.compensated").count() == 1
        assert session.query(LedgerEntry).count() == 0


def test_merchant_price_change_preserves_hold_until_explicit_reconfirmation(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'orders-price.db'}")
    load_seed(database)
    service = CapabilityService(database, enforce_prepare=True)
    reserve_prepared = service.execute("user_001", "X02", {"amount_minor": 10_000}, "price-reserve")
    reserved = service.confirm_capability("user_001", reserve_prepared.record_id,
                                          reserve_prepared.data["receipt_id"],
                                          reserve_prepared.data["action_hash"],
                                          reserve_prepared.data["display_hash"])
    quote = service.quote_merchant("user_001", "cake", service.clock.business_date())
    order = service.execute("user_001", "X04", {"delivery_date": service.clock.business_date(),
                                                 "amount_minor": quote["amount_minor"], "quote_id": quote["quote_id"],
                                                 "reservation_id": reserved.data["reservation_id"]}, "price-order")
    created = service.confirm_capability("user_001", order.record_id, order.data["receipt_id"],
                                         order.data["action_hash"], order.data["display_hash"])
    old_mandate_id = created.data["mandate_id"]
    service.update_merchant_catalog("cake", price_minor=8_000)
    paused = service.dispatch_due("user_001")[0]
    assert paused.status == "paused" and paused.data["error_code"] == "PRICE_CHANGED"
    with database.sessions() as session:
        assert session.get(Reservation, reserved.data["reservation_id"]).status == "active"
        assert session.get(Mandate, old_mandate_id).used_count == 0
        assert session.query(Operation).count() == 0
        assert session.query(LedgerEntry).count() == 0
    assert service.dispatch_due("user_001") == []
    reconfirmed = service.reconfirm_merchant_price("user_001", created.record_id, 8_000)
    assert reconfirmed.status == "awaiting_confirmation"
    confirmed = service.confirm_capability("user_001", created.record_id, reconfirmed.data["receipt_id"],
                                           reconfirmed.data["action_hash"], reconfirmed.data["display_hash"])
    assert confirmed.status == "scheduled"
    assert service.dispatch_due("user_001")[0].data["status"] == "delivered"
    with database.sessions() as session:
        assert session.get(Mandate, old_mandate_id).status == "revoked"
        assert session.get(Mandate, old_mandate_id).used_count == 0
        assert session.get(Mandate, confirmed.data["mandate_id"]).used_count == 1
        assert session.get(Reservation, reserved.data["reservation_id"]).status == "released"
    assert service._account("user_001").available_minor == 492_000


def test_aborting_price_reconfirmation_invalidates_quotes_and_releases_hold(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'orders-price-abort.db'}")
    load_seed(database)
    service = CapabilityService(database, enforce_prepare=True)
    reserve_prepared = service.execute("user_001", "X02", {"amount_minor": 10_000}, "abort-price-reserve")
    reservation = service.confirm_capability("user_001", reserve_prepared.record_id,
                                              reserve_prepared.data["receipt_id"],
                                              reserve_prepared.data["action_hash"],
                                              reserve_prepared.data["display_hash"])
    quote = service.quote_merchant("user_001", "flower", service.clock.business_date())
    prepared = service.execute("user_001", "X03", {
        "delivery_date": service.clock.business_date(), "dispatch_on": service.clock.business_date(),
        "amount_minor": quote["amount_minor"], "reservation_id": reservation.data["reservation_id"],
        "quote_id": quote["quote_id"],
    }, "abort-price-order")
    created = service.confirm_capability("user_001", prepared.record_id, prepared.data["receipt_id"],
                                         prepared.data["action_hash"], prepared.data["display_hash"])
    service.update_merchant_catalog("flower", price_minor=8_000)
    assert service.dispatch_due("user_001")[0].data["error_code"] == "PRICE_CHANGED"
    revised = service.reconfirm_merchant_price("user_001", created.record_id, 8_000)
    assert revised.status == "awaiting_confirmation"
    service.invalidate_prepared("user_001", revised.record_id, cancelled=True)
    with database.sessions() as session:
        assert session.query(Reservation).filter(Reservation.plan_id == "abort-price-reserve",
                                                 Reservation.status == "released").count() == 1
        assert session.get(Account, "acc_001").available_minor == 500_000


def test_confirming_price_reconfirmation_revokes_old_order_before_repricing(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'orders-price-confirm.db'}")
    load_seed(database)
    service = CapabilityService(database, enforce_prepare=True)
    reserve_prepared = service.execute("user_001", "X02", {"amount_minor": 10_000}, "confirm-price-reserve")
    reserved = service.confirm_capability("user_001", reserve_prepared.record_id,
                                          reserve_prepared.data["receipt_id"], reserve_prepared.data["action_hash"],
                                          reserve_prepared.data["display_hash"])
    quote = service.quote_merchant("user_001", "flower", service.clock.business_date())
    prepared = service.execute("user_001", "X03", {"delivery_date": service.clock.business_date(),
                                                     "dispatch_on": service.clock.business_date(),
                                                     "amount_minor": quote["amount_minor"],
                                                     "reservation_id": reserved.data["reservation_id"],
                                                     "quote_id": quote["quote_id"]}, "confirm-price-order")
    created = service.confirm_capability("user_001", prepared.record_id, prepared.data["receipt_id"],
                                         prepared.data["action_hash"], prepared.data["display_hash"])
    old_order_id = created.data["order_id"]
    old_mandate_id = created.data["mandate_id"]
    service.update_merchant_catalog("flower", price_minor=8_000)
    assert service.dispatch_due("user_001")[0].data["error_code"] == "PRICE_CHANGED"
    revised = service.reconfirm_merchant_price("user_001", created.record_id, 8_000)
    assert revised.status == "awaiting_confirmation"
    accepted = service.confirm_capability("user_001", revised.record_id, revised.data["receipt_id"],
                                          revised.data["action_hash"], revised.data["display_hash"])
    assert accepted.status == "scheduled"
    with database.sessions() as session:
        assert session.get(MerchantOrder, old_order_id).status == "scheduled"
        assert session.get(Mandate, old_mandate_id).status == "revoked"
        assert session.get(Reservation, reserved.data["reservation_id"]).status == "active"
        assert session.get(Account, "acc_001").available_minor == 490_000
    settled = service.dispatch_due("user_001")[0]
    assert settled.data["status"] == "delivered"
    assert service._account("user_001").ledger_balance_minor == 492_000
