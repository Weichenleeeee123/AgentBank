"""Pure, deterministic policy decisions. No LLM or database access is allowed here."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from agentbank.contracts import Assurance, Decision


@dataclass(frozen=True)
class PolicyContext:
    projected_today_minor: int = 0
    new_recipient: bool = False
    channel: str = "app"
    owner_ok: bool = True
    locked: bool = False
    facts: dict[str, Any] = field(default_factory=dict)


def action_hash(tool: str, args: dict[str, Any]) -> str:
    payload = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decide(operation: str, context: PolicyContext, *, operation_id: str = "op_pending") -> Decision:
    if not context.owner_ok:
        tier, assurance, executability, reasons = "D", Assurance.none, "deny", ["P02"]
    elif context.locked and operation not in {"account.balance", "transactions.query"}:
        tier, assurance, executability, reasons = "D", Assurance.none, "locked", ["U17"]
    elif operation in {"account.balance", "transactions.query"}:
        tier, assurance, executability, reasons = "G", Assurance.none, "allow", ["P03"]
    elif operation == "transfer.create":
        reasons = ["P05"]
        if context.projected_today_minor > 100_000:
            tier, assurance = "R", Assurance.mfa
            reasons = ["P06"]
        else:
            tier, assurance = "Y", Assurance.confirm
        if context.new_recipient:
            tier, assurance = "R", Assurance.mfa
            reasons.append("U05")
        executability = "allow"
    else:
        tier, assurance, executability, reasons = "D", Assurance.none, "deny", ["U21"]

    facts = {"projected_today_minor": context.projected_today_minor, "new_recipient": context.new_recipient}
    facts.update(context.facts)
    return Decision(
        decision_id=f"dec_{operation_id}",
        policy_version="2026-10-28.1",
        operation_id=operation_id,
        action_hash=action_hash(operation, facts),
        executability=executability,
        assurance=assurance,
        channel_requirement="app_only" if context.channel == "im" and assurance is Assurance.mfa else "any",
        tier_display=tier,
        reason_codes=reasons,
        facts=facts,
        warnings=[],
    )
