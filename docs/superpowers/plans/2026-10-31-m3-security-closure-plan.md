# M3 Security Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the locally verifiable M3 security loop so every sensitive write has risk, authorization, pre-commit recheck, audit evidence, and recoverable failure behavior.

**Architecture:** Extend the existing SQLite-backed security services instead of introducing a second execution kernel. Cooling, OTP lockout, breaker checks, DLP, fragment validation, and audit writes stay small and deterministic; `TransferService` and `CapabilityService` call the same pre-commit guards before their existing atomic business transactions. The self-test endpoint reports the existing evaluation matrix plus explicit audit and unauthorized-ledger gates.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Pydantic, pytest, Ruff, Mypy, YAML evaluation fixtures.

---

## Task 1: Add failing M3 state and security-boundary tests

**Files:**
- Create: `backend/tests/test_m3_security_closure.py`
- Modify: `backend/tests/test_m3_recovery_api.py`
- Modify: `backend/tests/test_m3_gate_controls.py`

- [ ] **Step 1: Write failing tests for cooling binding and OTP lockout**

Add tests that assert the public behavior before implementation:

```python
def test_cooling_requires_action_and_revision_and_has_reconfirm_window(database: Database) -> None:
    service = CoolingPeriodService(database, duration=timedelta(minutes=30), reconfirm_window=timedelta(minutes=10))
    view = service.start("user_001", "plan-1", 1000, action_hash="sha256:a", plan_revision=2,
                        now=datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc))
    with pytest.raises(DomainError, match="COOLING_PERIOD_ACTIVE"):
        service.release(view.id, action_hash="sha256:a", plan_revision=2,
                        now=datetime(2026, 10, 31, 0, 29, tzinfo=timezone.utc), reconfirmed=True)
    assert service.release(view.id, action_hash="sha256:a", plan_revision=2,
                           now=datetime(2026, 10, 31, 0, 35, tzinfo=timezone.utc), reconfirmed=True)
    with pytest.raises(DomainError, match="COOLING_RECONFIRMATION_REQUIRED"):
        service.release(view.id, action_hash="sha256:a", plan_revision=2,
                        now=datetime(2026, 10, 31, 0, 50, tzinfo=timezone.utc), reconfirmed=True)


def test_otp_lockout_persists_across_sessions_and_protective_write_survives(database: Database) -> None:
    service = DeviceService(database)
    token = service.register("user_001")
    challenge_id = service.issue_otp("user_001", "receipt-1", action_hash="sha256:a")
    for _ in range(3):
        with pytest.raises(DomainError, match="OTP_INVALID"):
            service.verify("user_001", "000000", receipt_id="receipt-1", action_hash="sha256:a")
    assert service.is_user_locked("user_001") is True
    assert service.validate_token("user_001", token) is None
```

Add boundary tests for app-session device access, OTP cross-receipt rejection, DLP on nested payloads, and self-test evidence fields.

- [ ] **Step 2: Run the focused tests and verify expected failures**

Run:

```powershell
python -m pytest backend/tests/test_m3_security_closure.py -q
```

Expected: collection succeeds and tests fail with missing keyword arguments or missing lockout methods. Do not implement until the failure is attributable to the missing M3 behavior.

- [ ] **Step 3: Commit the red tests**

```powershell
git add backend/tests/test_m3_security_closure.py backend/tests/test_m3_recovery_api.py backend/tests/test_m3_gate_controls.py
git commit -m "test: specify M3 security closure boundaries"
```

## Task 2: Extend the database model for security state

**Files:**
- Modify: `backend/agentbank/db.py:153-180,432-505`
- Test: `backend/tests/test_m3_security_closure.py`

- [ ] **Step 1: Add the minimum persisted fields required by the tests**

Extend `CoolingPeriod` with `plan_revision`, `action_hash`, and `reconfirm_deadline`. Add a `SecurityLock` model keyed by user with `locked_until`, `reason`, and `failed_attempts`. Extend `Challenge` with `action_hash` and `lockout_until` only if the implementation needs per-challenge expiry evidence; do not store plaintext OTP beyond the existing simulated delivery field used by the device page.

Use nullable/default-compatible columns so existing seed databases created by `Base.metadata.create_all` continue to initialize without migrations:

```python
plan_revision: Mapped[int] = mapped_column(Integer, default=1)
action_hash: Mapped[str] = mapped_column(String(128), default="")
reconfirm_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

- [ ] **Step 2: Run model and legacy security tests**

Run:

```powershell
python -m pytest backend/tests/test_m3_security.py backend/tests/test_m3_security_closure.py -q
```

Expected: old tests continue to pass; new tests still fail in service methods.

- [ ] **Step 3: Commit the schema change**

```powershell
git add backend/agentbank/db.py
git commit -m "feat: persist M3 cooling and lockout state"
```

## Task 3: Implement the real-time cooling-period state machine

**Files:**
- Modify: `backend/agentbank/security/cooling.py`
- Modify: `backend/agentbank/bank/service.py:142-240`
- Modify: `backend/agentbank/api/app.py:500-514`
- Test: `backend/tests/test_m3_security_closure.py`

- [ ] **Step 1: Implement `CoolingPeriodService.start` with hash/revision and injectable real time**

Keep the service API deterministic for tests:

```python
def start(self, user_id: str, plan_id: str, amount_minor: int, *, action_hash: str = "",
          plan_revision: int = 1, now: datetime | None = None) -> CoolingView:
    started = now or datetime.now(timezone.utc)
    expires = started + self.duration
    deadline = expires + self.reconfirm_window
    ...
```

Use UTC wall-clock values for all comparisons. Never read `BusinessClock` for cooling expiry.

- [ ] **Step 2: Implement release, invalidate, cancel, and status lookup**

`release` must validate owner-independent state, action hash, revision, active status, expiry, reconfirm deadline, and `reconfirmed=True`. Set `completed` and `reconfirmed_at` in one transaction. Add `invalidate` and `cancel` transitions that are idempotent and reject terminal-state reuse.

- [ ] **Step 3: Route transfer medium/unknown results through the service**

Replace the inline `CoolingPeriod(...)` construction in `TransferService.confirm` with `CoolingPeriodService.start`, passing the prepared plan action hash and revision. Return the cooling ID and preserve the pending operation without writing ledger entries. Make `/v1/cooling/{cooling_id}/release` accept optional `action_hash` and `plan_revision`, defaulting only for backward-compatible existing clients, and verify ownership before release.

- [ ] **Step 4: Run cooling tests and regression tests**

Run:

```powershell
python -m pytest backend/tests/test_m3_security.py backend/tests/test_m3_recovery_api.py backend/tests/test_m3_security_closure.py -q
```

Expected: cooling tests pass, existing API tests still pass, and no test can release during the active period or outside the ten-minute reconfirm window.

- [ ] **Step 5: Commit cooling behavior**

```powershell
git add backend/agentbank/security/cooling.py backend/agentbank/bank/service.py backend/agentbank/api/app.py backend/tests/test_m3_security_closure.py
git commit -m "feat: bind cooling release to action and revision"
```

## Task 4: Implement persistent OTP lockout and device binding

**Files:**
- Modify: `backend/agentbank/auth/devices.py`
- Modify: `backend/agentbank/bank/capabilities_full.py:190-230,560-620`
- Modify: `backend/agentbank/bank/service.py:209-240`
- Modify: `backend/agentbank/security/breaker.py`
- Test: `backend/tests/test_m3_security_closure.py`
- Test: `backend/tests/test_device_agent_binding.py`

- [ ] **Step 1: Implement persisted user lock state**

Add `DeviceService.record_otp_failure(user_id, reason)` and `is_user_locked(user_id, now=None)`. After the third failed attempt within ten real minutes, persist a fifteen-minute lock. Reset only after the lock expires; never reset on login or device re-pairing. Keep the existing three-attempt per-challenge guard.

- [ ] **Step 2: Bind challenges to action hash and enforce single-use**

Change `issue_otp` and `verify` to accept `action_hash`. Verify `receipt_id` and hash before incrementing attempts. On success set `consumed=True`; on failure increment both challenge attempts and the user-level lockout record. Return structured `OTP_EXPIRED`, `OTP_INVALID`, or `OTP_LOCKED` errors.

- [ ] **Step 3: Make sensitive execution honor the lock**

Before `CapabilityService.verify_mfa` and `TransferService.verify` commit any operation, call the user lock check. Keep the existing `CircuitBreaker.can_write(..., protective=True)` exception for freeze, loss, restriction-add, and limit-decrease operations. Ensure a failed MFA cannot mutate card or ledger state.

- [ ] **Step 4: Add API boundary tests**

Assert that no bearer-token endpoint can register a device, reset a token, or read an inbox; only `/admin/v1/devices/pairing-code` plus `/device/v1/pair` can create a device. Assert a device token for user A cannot read user B challenges.

- [ ] **Step 5: Run focused device and capability tests**

Run:

```powershell
python -m pytest backend/tests/test_device_agent_binding.py backend/tests/test_m3_security.py backend/tests/test_m3_security_closure.py backend/tests/test_m2_unified_authorization.py -q
```

- [ ] **Step 6: Commit OTP/device behavior**

```powershell
git add backend/agentbank/auth/devices.py backend/agentbank/security/breaker.py backend/agentbank/bank/capabilities_full.py backend/agentbank/bank/service.py backend/tests/test_m3_security_closure.py backend/tests/test_device_agent_binding.py
git commit -m "feat: persist OTP lockout and bind device challenges"
```

## Task 5: Close the common commit and audit gate

**Files:**
- Modify: `backend/agentbank/bank/capabilities_full.py`
- Modify: `backend/agentbank/bank/service.py`
- Modify: `backend/agentbank/audit/store.py`
- Modify: `backend/agentbank/security/dlp.py`
- Modify: `backend/agentbank/security/fragments.py`
- Modify: `backend/agentbank/security/explain.py`
- Test: `backend/tests/test_m3_security_closure.py`

- [ ] **Step 1: Write failing atomicity tests**

Add tests that prepare a transfer, mutate the persisted node argument or display data before commit, and assert `HASH_MISMATCH` plus zero new `LedgerEntry` rows. Add an audit-failure test that injects an append failure and asserts the business transaction rolls back. Add fragment tests for Arabic/Chinese amount text, status claims, and mismatched financial labels.

- [ ] **Step 2: Implement a reusable pre-commit guard**

Extract a small private helper in the existing services that checks owner, receipt state, expiry, action/display hashes, lock state, and audit availability before the ledger write. Keep it deterministic and side-effect free. Invoke it inside the same `BEGIN IMMEDIATE`/`database.atomic()` transaction used by the existing commit path.

- [ ] **Step 3: Make audit checkpoint verification detect missing/tampered tails**

Retain the existing hash-chain behavior and add a `checkpoint_and_verify()` helper that creates a final checkpoint then verifies all records. Return false or raise `AUDIT_UNAVAILABLE` when a checkpoint cannot be written. Do not remove existing audit rows or checkpoint rows.

- [ ] **Step 4: Expand DLP and fragment validation**

Redact nested card numbers, phone numbers, OTP labels and standalone six-digit OTP values in payloads. Reject text fragments containing Arabic or Chinese numeric amounts, dates, operation status claims, account/object identifiers, or financial labels; keep fact/status rendering as the only source of those values.

- [ ] **Step 5: Run atomicity and legacy suites**

Run:

```powershell
python -m pytest backend/tests/test_m1_audit.py backend/tests/test_m2_atomic_commit.py backend/tests/test_m3_security.py backend/tests/test_m3_security_closure.py -q
```

- [ ] **Step 6: Commit the common gate**

```powershell
git add backend/agentbank/bank/capabilities_full.py backend/agentbank/bank/service.py backend/agentbank/audit/store.py backend/agentbank/security/dlp.py backend/agentbank/security/fragments.py backend/agentbank/security/explain.py backend/tests/test_m3_security_closure.py
git commit -m "feat: enforce M3 commit and evidence gates"
```

## Task 6: Make the self-test a real M3 gate

**Files:**
- Modify: `backend/agentbank/api/app.py:730-751`
- Modify: `backend/agentbank/evals/runner.py`
- Modify: `backend/agentbank/evals/traceability.py`
- Modify: `evals/m3_recovery.yaml`
- Modify: `evals/security_matrix.yaml`
- Test: `backend/tests/test_m3_gate_controls.py`
- Test: `backend/tests/test_m3_eval_runner.py`

- [ ] **Step 1: Add failing self-test assertions**

Assert that `/admin/v1/self-test` returns non-empty SEC/FP/M3 case results, `audit_verified`, `unauthorized_ledger_writes == 0`, a suite hash, and a final checkpoint identifier. Assert an empty or malformed M3 suite returns 503 or a failed gate instead of a false green result.

- [ ] **Step 2: Extend the evaluation report with category and gate data**

Keep the existing `EvaluationReport` shape compatible, but add serialized case results and category counts. Add a deterministic helper that records ledger entries created by security cases and classifies any write without a completed receipt as unauthorized.

- [ ] **Step 3: Populate explicit recovery fixtures**

Add YAML cases for unknown idempotency preservation, Saga reverse compensation, cooling active/expired/changed-action outcomes, breaker protective exception, device isolation, fragment rejection, and DLP. Keep the existing 223 cases and add new cases rather than replacing files.

- [ ] **Step 4: Return and enforce evidence from the API**

Run the suite in a fresh demo database, create a final audit checkpoint, verify the chain, and include the evidence fields in the response. Mark the `DemoRun` failed when any gate fails. Preserve admin authentication and the existing traceability manifest.

- [ ] **Step 5: Run self-test and evaluation tests**

Run:

```powershell
python -m pytest backend/tests/test_m3_gate_controls.py backend/tests/test_m3_eval_runner.py backend/tests/test_m3_recovery_api.py -q
python -c "from pathlib import Path; from agentbank.evals.runner import run_cases; r=run_cases(Path('evals')); print(r.total, r.passed, r.failed)"
```

Expected: all existing and new cases pass; the reported suite is non-empty and its content hash is stable for an unchanged checkout.

- [ ] **Step 6: Commit the self-test gate**

```powershell
git add backend/agentbank/api/app.py backend/agentbank/evals/runner.py backend/agentbank/evals/traceability.py evals/m3_recovery.yaml evals/security_matrix.yaml backend/tests/test_m3_gate_controls.py backend/tests/test_m3_eval_runner.py
git commit -m "feat: make M3 self-test enforce security evidence"
```

## Task 7: Update M3 evidence and complete verification

**Files:**
- Modify: `docs/M3-COVERAGE.md`
- Modify: `docs/M2-M3-STATUS.md`
- Modify: `docs/security-self-eval.md`
- Test: `tests/test_deployment_smoke.py`

- [ ] **Step 1: Run the full verification set**

Run:

```powershell
python -m pytest -q
python -m ruff check backend
python -m mypy backend/agentbank
python -c "from pathlib import Path; from agentbank.evals.runner import run_cases; r=run_cases(Path('evals')); assert r.failed == 0; print({'total': r.total, 'passed': r.passed, 'failed': r.failed})"
python -c "import yaml; yaml.safe_load(open('deploy/docker-compose.yml', encoding='utf-8')); print('compose yaml ok')"
```

Expected: all tests pass, Ruff and Mypy are clean, the evaluation runner has zero failures, and compose YAML parses.

- [ ] **Step 2: Update evidence with measured results only**

Record the actual test count, evaluation total, security category counts, audit verification, unauthorized-ledger count, and known simulation boundaries. Do not copy target percentages into the report as results.

- [ ] **Step 3: Review the diff without deleting files**

Run:

```powershell
git diff --check
git status --short
git diff --stat HEAD~6..HEAD
```

Confirm no existing file was deleted and no secret value was added.

- [ ] **Step 4: Commit the evidence update**

```powershell
git add docs/M3-COVERAGE.md docs/M2-M3-STATUS.md docs/security-self-eval.md
git commit -m "docs: record M3 security closure evidence"
```



## Execution record — 2026-10-31

Tasks 1–7 were executed inline in `codex/m3-security-closure`. The original implementation outline was refined after negative tests exposed gaps: cooling now reserves funds and signs a new short-lived MFA receipt; transfer MFA rechecks risk and hashes; authenticated API forgery automatically feeds U17; U18 applies only to infrastructure failures; external HMAC audit anchors survive database-only truncation; SEC metrics come from the actual per-case ledger rather than the host database.

Final verification: 538 pytest tests passed, Ruff and Mypy passed, 227/227 evaluation cases passed, eight SEC categories each contain ten cases, HTTP self-test and report reload passed, Node syntax checks passed, and Compose YAML parsed. Exact commands, measured performance, remaining simulation boundaries, and raw evidence are in `docs/M3-LOCAL-EVIDENCE.md` and `docs/evidence/m3-local/`. No file deletion was performed; the external worktree is preserved.
