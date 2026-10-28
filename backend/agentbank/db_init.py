from __future__ import annotations

import argparse

from agentbank.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Create AgentBank SQLite schema")
    parser.add_argument("--db", default="sqlite:///agentbank.db")
    args = parser.parse_args()
    Database(args.db).create_schema()
    print(f"schema initialized: {args.db}")


if __name__ == "__main__":
    main()
