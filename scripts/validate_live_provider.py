"""Opt-in live-provider smoke using synthetic seed data; never prints credentials."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database, LedgerEntry, Payee, PlanNodeRecord
from agentbank.llm.gateway import LLMGateway
from agentbank.seed.loader import load_seed
from sqlalchemy import select


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output exists; use a new evidence path.")
    database = Database("sqlite:///:memory:")
    load_seed(database)
    gateway = LLMGateway.from_environment(database)
    if gateway.primary is None:
        raise SystemExit("Primary provider is not configured.")
    calls: list[dict[str, object]] = []

    def observed(config, role, payload):
        started = time.monotonic()
        row: dict[str, object] = {"role": role, "provider": config.name}
        try:
            result = LLMGateway._http_transport(config, role, payload)
            row["status"] = "received_json"
            return result
        except Exception as error:
            row.update(status="failed", error_type=type(error).__name__)
            raise
        finally:
            row["seconds"] = round(time.monotonic() - started, 3)
            calls.append(row)

    gateway._provider_transport = observed
    roles: dict[str, bool] = {}
    plan = gateway.plan_intent("查余额", user_id="user_001")
    roles["planner"] = plan.get("capability_id") == "Q01" and plan.get("source") != "deterministic_fallback"
    facts = [{"type": "fact", "label": "可用余额", "value_minor": 500000, "currency": "CNY"}]
    reply = gateway.render_reply("user_001", "Q01", "succeeded", {"available_minor": 500000}, facts)
    roles["reply"] = reply.get("source") not in {"template_fallback", "deterministic_fallback"} and reply.get("fragments") == facts
    observations = gateway.explore_read_only("user_001", "balance", {}, max_steps=1,
                                            executor=lambda *_: {"available_minor": 500000, "currency": "CNY"})
    roles["explorer"] = bool(observations)
    fraud = gateway.classify_fraud("给房东转20元交房租", user_id="user_001")
    roles["fraud"] = fraud.get("category") == "safe" and fraud.get("source") != "deterministic_fallback"
    agent = AgentOrchestrator(database, llm=gateway, capabilities=CapabilityService(
        database, require_device_mfa=True, enforce_prepare=True))
    with database.sessions() as session:
        before_ledger = len(session.scalars(select(LedgerEntry)).all())
    journeys: list[dict[str, object]] = []
    for index, (text, expected) in enumerate([
        ("查余额", "Q01"), ("给房东转账", "T01"), ("20元", "T01"),
        ("查询我的理财持仓", "W06"), ("查询我的订阅", "S01"),
        ("查询我的交易记录", "Q02"),
    ]):
        conversation = "live-transfer" if index in {1, 2} else f"live-{index}"
        started = time.monotonic()
        try:
            result = agent.handle("user_001", conversation, text)
            passed = result.capability_id == expected
            if index in {1, 2}:
                passed = result.capability_id in {'T01', 'T03'} and result.kind == (
                    'clarification' if index == 1 else 'confirmation')
            if index == 2 and passed:
                with database.sessions() as session:
                    node = session.scalar(select(PlanNodeRecord).where(
                        PlanNodeRecord.plan_id == result.data.get('plan_id')))
                    node_args = json.loads(node.args_json) if node else {}
                    payee = session.get(Payee, node_args.get('recipient_id', ''))
                    passed = (node_args.get('amount_minor') == 2000 and payee is not None
                              and payee.owner_user_id == 'user_001' and payee.display_name == '房东')
            journeys.append({"case": index + 1, "kind": result.kind,
                             "capability_id": result.capability_id,
                             "passed": passed,
                             "seconds": round(time.monotonic() - started, 3)})
        except Exception as error:
            journeys.append({"case": index + 1, "passed": False, "error_type": type(error).__name__})
        print(f"Live synthetic case {index + 1}: {'passed' if journeys[-1]['passed'] else 'failed'}", flush=True)
    with database.sessions() as session:
        unchanged_ledger = len(session.scalars(select(LedgerEntry)).all()) == before_ledger
    evidence = {"created_at": datetime.now(timezone.utc).isoformat(),
                "scope": "synthetic local seed; real primary HTTP; no money execution",
                "configured_backup": gateway.backup is not None,
                "primary_timeout_seconds": gateway.primary.timeout_seconds,
                "role_checks": roles, "reply_source": reply.get("source"), "journeys": journeys, "calls": calls,
                "call_counts": dict(Counter(str(row["role"]) for row in calls)),
                "ledger_unchanged": unchanged_ledger,
                "all_passed": all(roles.values()) and all(row["passed"] for row in journeys) and unchanged_ledger}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"role_checks": roles, "all_passed": evidence["all_passed"]}))
    raise SystemExit(0 if evidence["all_passed"] else 1)


if __name__ == "__main__":
    # Environment is supplied by the caller; this script never opens or echoes .env.
    os.environ.setdefault("AGENTBANK_AUDIT_ANCHOR_DIR", ".tmp/live-provider-anchors")
    main()
