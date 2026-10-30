from __future__ import annotations

from typing import Any

from agentbank.bank.capabilities_full import CapabilityService


class BusinessScheduler:
    """One idempotent scheduler tick for due business work in one app instance."""

    def __init__(self, capabilities: CapabilityService) -> None:
        self.capabilities = capabilities

    def tick(self) -> dict[str, Any]:
        orders = self.capabilities.dispatch_due()
        investments = self.capabilities.progress_investments(due_only=True)
        reminders = self.capabilities.dispatch_reminders()
        delivered = self.capabilities.deliver_reminders()
        cards = self.capabilities.advance_business_state()
        return {"orders_dispatched": len(orders), "investments_settled": len(investments),
                "reminders_scheduled": len(reminders), "reminders_delivered": len(delivered),
                "cards_progressed": len(cards)}
