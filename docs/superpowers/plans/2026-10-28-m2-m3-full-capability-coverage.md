# M2-M3 Full Capability Coverage Implementation Plan

> **For agentic workers:** Implement inline in this session with TDD and frequent commits.

**Goal:** Complete all 33 PLAN capabilities with real simulated business state, API endpoints, audit evidence, compensations, and M3 recovery/security integration.

**Architecture:** Add a persisted capability record layer for domain objects not needed by M1 ledger, and a single `CapabilityService` that enforces ownership, assurance, idempotency, state transitions, and audit events. Keep M1 transfer authorization as the canonical money-write path; route all other commands through the same policy/security boundary. Expose deterministic REST/Web IM surfaces and executable CAP/SEC/FP evaluation cases.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, SQLite, pytest, YAML.

---

### Task 1: Capability persistence and contracts

**Files:** `backend/agentbank/db.py`, `backend/agentbank/contracts.py`, `backend/agentbank/bank/capabilities_full.py`, `backend/tests/test_m2_full_capabilities.py`

- Add a generic persisted `CapabilityRecord` with `kind`, `user_id`, `status`, `data_json`, `idempotency_key`, timestamps and unique idempotency key.
- Add strict request/response contracts for schedule, AA, investments, cards, subscriptions, merchant orders and security forms.
- Write failing tests for persisted idempotency and ownership.

### Task 2: Query and transfer capabilities

**Files:** `backend/agentbank/bank/capabilities_full.py`, `backend/agentbank/api/app.py`, tests.

- Implement Q01/Q02 evidence responses including `as_of` and user scoping.
- Implement T02 phone transfer preparation, T03 alias resolution, T04 scheduled one-shot/recurring instances with per-period operation IDs and duplicate dispatch protection, T05 AA preview/create/status with participant-owned requests only.
- Every command writes audit events and returns business state.

### Task 3: Analysis and wealth capabilities

**Files:** `backend/agentbank/analysis/insights.py`, `backend/agentbank/bank/capabilities_full.py`, API, tests.

- Implement B01-B04, including yearly reports with missing months, W01-W06 including risk expiry, compare, subscribe, redeem processing, and positions with source/as-of evidence.
- Use the generic record layer for investment orders/positions and keep suitability deterministic.

### Task 4: Card, subscription and cross-scenario capabilities

**Files:** capability service, API, tests.

- Implement C01-C08 state machines and transaction blocking; S01-S03 detection/reminders/cancellation with debit authorization state; X01-X05 birthday discovery, reservations, merchant quote/order, Saga compensation, and freeze-then-loss-report flow.

### Task 5: M3 security integration

**Files:** `bank/service.py`, `policy/engine.py`, `api/app.py`, security/workflow modules, tests.

- Enforce fraud assessment before transfer commit, cooling periods for medium risk, circuit breaker for write commands, reply fragment validation, unknown recovery and human handoff.
- Add operation idempotency and audit events for every outcome, including denied/unknown/compensated.

### Task 6: Evaluation and release evidence

**Files:** `evals/`, `backend/agentbank/evals/runner.py`, tests, `docs/PLAN.md` appendix A if needed.

- Add at least three CAP cases per capability plus SEC/FP cases, run them against the service, and report measured numerator/denominator, latency, failures and known limitations.
- Run full pytest, Ruff, Mypy, diff check; commit each task separately.

---

**Coverage gate:** Do not claim M2/M3 complete until every ID Q01–X05 has a callable handler, persisted business-state assertion, audit assertion, and three CAP cases.
