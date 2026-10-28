"""Versioned contracts shared by the planner, policy engine and bank core."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Assurance(StrEnum):
    none = "none"
    confirm = "confirm"
    mfa = "mfa"


class ToolKind(StrEnum):
    query = "query"
    command = "command"


class OperationStatus(StrEnum):
    accepted = "accepted"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"
    unknown = "unknown"


class TransferCreateRequest(Contract):
    source_account_id: str = Field(min_length=1)
    recipient_id: str = Field(min_length=1)
    amount_minor: StrictInt = Field(gt=0)
    currency: str = Field(default="CNY", pattern="^CNY$")
    memo: str | None = Field(default=None, max_length=80)


class TransactionQueryRequest(Contract):
    start_date: str | None = None
    end_date: str | None = None
    category: str | None = None
    limit: StrictInt = Field(default=100, gt=0, le=50_000)


class AnalysisDslRequest(Contract):
    source: str = Field(default="transactions", pattern="^transactions$")
    filters: list[dict[str, Any]] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    limit: StrictInt = Field(default=100, gt=0, le=50_000)


class RiskQuestionnaireRequest(Contract):
    answers: list[StrictInt] = Field(min_length=10, max_length=10)


class CardFreezeRequest(Contract):
    card_id: str | None = None


class SubscriptionCancelRequest(Contract):
    subscription_id: str = Field(min_length=1)


class AASplitRequest(Contract):
    total_minor: StrictInt = Field(gt=0)
    participant_ids: list[str] = Field(min_length=2)


class MandateRequest(Contract):
    plan_id: str
    operation_type: str
    subject_ref: str
    max_amount_minor: StrictInt = Field(gt=0)
    max_uses: StrictInt = Field(gt=0)
    starts_on: str
    expires_on: str


class CapabilityCommandRequest(Contract):
    capability_id: str = Field(pattern="^[QTBWCSX][0-9]{2}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=160)


class CapabilityResult(Contract):
    capability_id: str
    record_id: str
    status: str
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)


class OperationMetadata(Contract):
    operation_id: str
    idempotency_key: str
    plan_revision: int = Field(ge=1)


class ToolDefinition(Contract):
    name: str
    version: str
    kind: ToolKind
    input_schema: dict[str, Any]
    security: dict[str, Any]
    preconditions: list[str] = Field(default_factory=list)
    execution: dict[str, Any]
    untrusted_output_fields: list[str] = Field(default_factory=list)
    evidence_fields: list[str] = Field(default_factory=list)


class ToolResult(Contract):
    operation_id: str
    status: OperationStatus
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


class IntentReference(Contract):
    slot: str
    source: str
    text: str


class IntentFrame(Contract):
    intent: str
    slots: dict[str, Any] = Field(default_factory=dict)
    references: list[IntentReference] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    suggested_next_action: str


class PlanNode(Contract):
    id: str
    tool: str
    args: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)
    compensation: str | None = None
    timeout_ms: int = Field(default=5_000, gt=0)
    idempotency_key: str


class Plan(Contract):
    plan_id: str
    revision: int = Field(ge=1)
    status: str
    user_id: str
    nodes: list[PlanNode] = Field(min_length=1, max_length=20)
    display: list[dict[str, str]] = Field(default_factory=list)
    created_at: datetime


class Decision(Contract):
    decision_id: str
    policy_version: str
    operation_id: str
    action_hash: str
    executability: str
    assurance: Assurance
    channel_requirement: str
    tier_display: str = Field(pattern="^[GYRD]$")
    reason_codes: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class Receipt(Contract):
    receipt_id: str
    subject_id: str
    plan_id: str
    plan_revision: int
    node_ids: list[str] = Field(min_length=1)
    action_hash: str
    display_hash: str
    decision_ids: list[str] = Field(min_length=1)
    assurance: Assurance
    issued_at: datetime
    expires_at: datetime


class AuditEvent(Contract):
    event_id: str
    seq: int = Field(ge=1)
    trace_id: str
    subject_id: str
    actor_type: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str | None = None
    event_hash: str
    occurred_at: datetime
