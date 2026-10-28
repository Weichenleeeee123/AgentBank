from __future__ import annotations

import argparse

from agentbank.auth.devices import DeviceService
from agentbank.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Pair a simulated phone outside the app session")
    parser.add_argument("--db", default="sqlite:///agentbank.db")
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    database = Database(args.db)
    database.create_schema()
    token = DeviceService(database).register(args.user)
    print(token)


if __name__ == "__main__":
    main()
