from __future__ import annotations

from typing import cast

from agentbank.db import Account, Database, Payee, User
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
