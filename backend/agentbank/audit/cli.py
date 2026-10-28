from __future__ import annotations

import argparse

from agentbank.audit.store import PersistentAudit
from agentbank.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify AgentBank audit hash chain")
    parser.add_argument("--db", default="sqlite:///agentbank.db")
    parser.add_argument("command", choices=["verify", "checkpoint"])
    args = parser.parse_args()
    audit = PersistentAudit(Database(args.db))
    if args.command == "checkpoint":
        checkpoint = audit.checkpoint()
        print(checkpoint.id if checkpoint else "no-events")
    else:
        print("ok" if audit.verify() else "invalid")
        raise SystemExit(0 if audit.verify() else 1)


if __name__ == "__main__":
    main()
