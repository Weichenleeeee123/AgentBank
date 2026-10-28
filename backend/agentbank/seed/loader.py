from __future__ import annotations

from typing import cast

from agentbank.db import Account, Card, Database, Event, Payee, Product, Subscription, Transaction, User
from agentbank.seed.generator import generate_seed


def load_seed(database: Database) -> None:
    database.create_schema()
    fixture = generate_seed()
    with database.sessions.begin() as session:
        if session.get(User, "user_001") is not None:
            return
        session.add_all(User(id=str(user["id"]), display_name=str(user["display_name"])) for user in fixture["users"])
        session.add_all(Account(id=str(item["id"]), user_id=str(item["user_id"]),
                                available_minor=cast(int, item["available_minor"]), ledger_balance_minor=cast(int, item["ledger_balance_minor"]))
                         for item in fixture["accounts"])
        session.add_all(Payee(id=str(item["id"]), owner_user_id=str(item["owner_user_id"]),
                              display_name=str(item["display_name"]), masked_name=str(item["masked_name"]),
                              phone=item["phone"], account_id=str(item["account_id"]), is_new=bool(item["is_new"]))
                         for item in fixture["payees"])
        session.add_all(Transaction(id=str(item["id"]), user_id=str(item["user_id"]), posted_at=str(item["posted_at"]),
                                    amount_minor=int(cast(int, item["amount_minor"])), merchant=str(item["merchant"]),
                                    category=str(item.get("category", "其他")), direction=str(item.get("direction", "debit")),
                                    memo=item.get("memo")) for item in fixture["transactions"])
        session.add_all(Product(id=str(item["id"]), name=str(item["name"]), risk_level=str(item["risk_level"]),
                                term_days=int(cast(int, item.get("term_days", 0))), fee_bps=int(cast(int, item.get("fee_bps", 0))),
                                redemption_days=int(cast(int, item.get("redemption_days", 1)))) for item in fixture["products"])
        session.add_all(Card(id=str(item["id"]), user_id=str(item["user_id"]), status=str(item["status"]))
                         for item in fixture["cards"])
        session.add_all(Subscription(id=str(item["id"]), user_id=str(item["user_id"]), merchant=str(item["merchant"]),
                                     amount_minor=int(cast(int, item["amount_minor"])), status=str(item["status"]),
                                     next_charge_at=str(item["next_charge_at"])) for item in fixture["subscriptions"])
        session.add_all(Event(id=str(item["id"]), user_id=str(item["user_id"]), event_type=str(item["event_type"]),
                              event_date=str(item["event_date"]), title=str(item["title"])) for item in fixture["events"])
