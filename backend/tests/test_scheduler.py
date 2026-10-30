from pathlib import Path

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.clock import utc_now_naive
from agentbank.db import CardApplication, Database, MerchantOrder, Subscription, UserNotification
from agentbank.scheduler import BusinessScheduler
from agentbank.seed.loader import load_seed


def test_scheduler_tick_dispatches_orders_reminders_and_card_lifecycle(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'scheduler.db'}")
    load_seed(database)
    service = CapabilityService(database)
    card = service.execute("user_001", "C01", {"virtual": False, "assurance": "mfa"}, "scheduler-card")
    service.execute("user_001", "S02", {"subscription_id": "sub_video", "days": 3}, "scheduler-reminder")
    with database.sessions.begin() as session:
        subscription = session.get(Subscription, "sub_video")
        subscription.next_charge_at = utc_now_naive().date().isoformat()
    order = service.execute("user_001", "X03", {"delivery_date": service.clock.business_date(),
                                                    "dispatch_on": service.clock.business_date(),
                                                    "amount_minor": 100}, "scheduler-order")

    summary = BusinessScheduler(service).tick()

    assert summary["cards_progressed"] >= 1
    assert summary["orders_dispatched"] == 1
    assert summary["reminders_delivered"] == 1
    with database.sessions() as session:
        application = session.query(CardApplication).filter(CardApplication.card_id == card.data["card_id"]).one()
        stored_order = session.get(MerchantOrder, order.data["order_id"])
        notification = session.query(UserNotification).one()
        assert application.status == "reviewing"
        assert stored_order.status == "delivered"
        assert notification.status == "unread"
