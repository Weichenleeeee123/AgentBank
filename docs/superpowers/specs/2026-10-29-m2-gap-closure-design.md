# M2 Gap Closure Design

**Goal:** Close every gap listed in the M2 audit table with executable code, independent business-state tests, and staged evidence.

## Coverage Mapping

### Stage 1: Agent and LLM runtime

- PLAN section 6: structured `IntentFrame`, planner output, model gateway roles, prompt versions, bounded read-only exploration, and structured reply fragments.
- Natural-language coverage: all 33 capabilities, multi-turn slot completion, ambiguity handling, references, correction, cancellation, and terminal business results.
- Safety boundary: model receives redacted structured context, never write-tool handles or untrusted free-text fields; deterministic parser remains a fallback when no provider is configured.

### Stage 2: Business and channel closure

- Web IM/App shared confirmation, callback nonce, red operation App-only handoff, and result delivery.
- X01 trusted-event plus historical merchant discovery and notification.
- W04/W05 order, position, valuation, settlement, and natural-language redemption selection.
- X03/X04 quote, bounded mandate, reservation, due dispatch, price/amount re-confirmation, Saga compensation, and recovery.
- S02 reminder scheduling, outbox consumption, delivery status, retry, and user-visible receipt.
- C01 physical-card application, review, production, delivery, rejection, and retry states.
- Cross-cutting receipt, action/display hash, mandate, ledger, idempotency, audit, and state-recovery invariants.

### Stage 3: Acceptance and release closure

- 33 x 3 independent CAP journeys with business terminal-state assertions, per-case audit evidence, and Appendix A traceability.
- HTTP and Agent dual-channel end-to-end matrix, including confirmation, MFA, replay, cross-user isolation, and recovery.
- Attack/false-positive matrix linkage, evaluator panel evidence, Docker build/start/health, and local reproduction checks.

## Completion Rule

An item is closed only when its implementation, endpoint or Agent path, persistent state, audit evidence, and focused regression test are present. Passing the existing generated `223/223` matrix alone does not close an item.
