# M2 Gap Closure Implementation Plan

> **For agentic workers:** Execute each task with test-first development and commit at every stage boundary.

**Goal:** Implement and verify every item in the M2 audit gap table.

**Architecture:** Keep `CapabilityService` as the authoritative business-state kernel. Add a structured Agent planner layer above it, a bounded notification/channel layer beside it, and an explicit acceptance harness below it. Model output can propose structured data only; deterministic validation and business services decide all side effects.

**Tech Stack:** Python, FastAPI, SQLAlchemy/SQLite, pytest, existing LLM gateway, existing PWA/Web IM surfaces.

---

### Task 1: Structured Agent runtime

**Files:** `backend/agentbank/llm/gateway.py`, `backend/agentbank/agent/orchestrator.py`, `backend/agentbank/contracts.py`, `backend/tests/test_agent_core.py`, `backend/tests/test_llm_gateway.py`

- [ ] Add failing tests for planner-role schema output, structured slot merge, bounded read-only exploration, and reply-fragment fallback.
- [ ] Add gateway role methods with prompt version, provider/model audit, redacted context, one retry, and deterministic fallback.
- [ ] Route planner output through existing parser, payload validator, policy, receipt, and capability kernel.
- [ ] Verify Agent flows for all capability families and commit Stage 1.

### Task 2: Natural-language terminal journeys

**Files:** `backend/agentbank/agent/intent.py`, `backend/agentbank/agent/orchestrator.py`, `backend/tests/test_agent_core.py`

- [ ] Add failing tests for reminders, references, correction, cancellation, redemption selection, and order recovery.
- [ ] Implement persistent structured references and pending-plan transitions without storing assistant free text.
- [ ] Verify each journey reaches a terminal business state or an explicit safe handoff.

### Task 3: Business and channel closure

**Files:** `backend/agentbank/bank/capabilities_full.py`, `backend/agentbank/db.py`, `backend/agentbank/api/app.py`, `backend/tests/test_m2_*`, `backend/tests/test_webim_callbacks.py`

- [ ] Add failing tests for X01 history-triggered discovery, W05 position selection/valuation, X03/X04 quote and compensation, S02 delivery/retry receipt, and C01 rejection/retry lifecycle.
- [ ] Implement persistent states, outbox consumption, cross-channel callbacks, and recovery without bypassing authorization.
- [ ] Verify app and Web IM flows and commit Stage 2.

### Task 4: Independent CAP and traceability harness

**Files:** `backend/agentbank/evals/runner.py`, `evals/`, `backend/tests/test_capability_matrix.py`, `docs/M2-COVERAGE.md`, Appendix A files

- [ ] Add independent normal, colloquial/reference, and boundary/recovery cases for each capability.
- [ ] Assert domain terminal state, ledger/receipt/audit evidence, user isolation, and replay behavior per case.
- [ ] Emit case-to-test-to-code traceability and commit Stage 3 acceptance evidence.

### Task 5: Release and deployment verification

**Files:** `docker-compose.yml`, `Dockerfile*`, `README*`, CI configuration, deployment docs

- [ ] Add smoke checks for Docker build, two-instance startup, `/healthz`, evaluator access, and clean-environment reproduction.
- [ ] Run the full test, lint, type, eval, and deployment checks; record results and commit Stage 3 release evidence.
