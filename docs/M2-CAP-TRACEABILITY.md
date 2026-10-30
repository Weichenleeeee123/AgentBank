# M2 CAP Traceability

The M2 capability matrix is a deterministic acceptance suite. `run_cases` expands
`evals/m2_m3_capability_matrix.yaml` into 33 isolated capabilities x 3 journeys
(99 cases). Every case returns a case ID, branch, persistent terminal-state
evidence, audit event, and errors. A case that raises is failed; unknown case
kinds are rejected.

| CAP | Normal branch | Colloquial branch | Boundary/recovery evidence |
|---|---|---|---|
| Q01 | strict prepare; balance fields | Agent conversation | idempotent replay |
| Q02 | strict query | Agent conversation | idempotent replay |
| T01 | prepare/confirm ledger | Agent conversation | ambiguous payee denied |
| T02 | prepare/confirm ledger | Agent conversation | unknown phone denied |
| T03 | prepare/confirm ledger | Agent conversation | ambiguous alias denied |
| T04 | mandate schedule | Agent conversation | two periods, distinct operations, dedupe |
| T05 | AA bill and shares | Agent conversation | cent-exact split |
| B01 | DSL rows | Agent conversation | idempotent replay |
| B02 | anomaly rows | Agent conversation | idempotent replay |
| B03 | monthly report | Agent conversation | idempotent replay |
| B04 | annual report | Agent conversation | idempotent replay |
| W01 | risk-bounded products | Agent conversation | idempotent replay |
| W02 | objective comparison | Agent conversation | idempotent replay |
| W03 | persisted risk profile | Agent multi-round questionnaire | invalid answers denied |
| W04 | subscribed order/position | Agent confirmation + OTP | suitability mismatch denied |
| W05 | processing redemption | Agent conversation | unredeemable order denied |
| W06 | source/as-of P&L | Agent conversation | idempotent replay |
| C01 | card application progressed | Agent conversation | physical card remains pending |
| C02 | limit + payment rejection | Agent confirmation + OTP | product limit denied |
| C03 | restriction + payment block | Agent conversation | restriction transition |
| C04 | restriction removed + payment | Agent confirmation + OTP | restriction transition |
| C05 | lost card + payment block | Agent confirmation + OTP | protective state |
| C06 | released card + payment | Agent confirmation + OTP | release requires lost state |
| C07 | frozen card + payment block | Agent conversation | protective state |
| C08 | PIN changed, secret absent | secure form direct (PIN never chat) | form-required boundary |
| S01 | confirmed/suspected subscriptions | Agent conversation | idempotent replay |
| S02 | scheduled reminder | Agent conversation | reminder deduped |
| S03 | cancelled subscription + revoked mandate | Agent conversation | cancelled subscription cannot charge |
| X01 | trusted event recommendation | Agent conversation | idempotent replay |
| X02 | active funds reservation | Agent conversation | reservation persists until release |
| X03 | delivered flower order | Agent conversation | merchant failure compensation |
| X04 | delivered cake order | Agent conversation | merchant failure compensation |
| X05 | lost card after freeze/OTP | Agent conversation | OTP failure keeps card frozen |

The normal and colloquial branches use a fresh SQLite database seeded from the
repeatable fixture. Writes run through `CapabilityService` with
`enforce_prepare=True` and `require_device_mfa=True`; the runner consumes the
confirmation and simulated device OTP. Colloquial journeys use the deterministic
`AgentOrchestrator` parser and its real multi-round continuation. C08 is the
single documented exception: the secure form is used so a PIN never enters chat
state or audit payloads.

Boundary journeys assert the capability-specific business result, such as ledger
balance, card payment authorization, subscription mandate state, merchant order
settlement, or a persisted refusal. They do not count a generic record or audit
boolean as success.

## Executable manifest

The machine-readable manifest is built by
`agentbank.evals.traceability.build_traceability_manifest` and validated by
`validate_traceability_manifest`. It expands the canonical runner inventory into
all 99 case IDs and records, for each case, the PLAN sections, implementation
symbol, runner function, pytest function, and the PLAN §11 boundary-fixture
mapping. Missing capability-specific boundary tests are marked `missing`; the
manifest never infers coverage from a generic test.

`build_traceability_artifact(report, root)` binds an `EvaluationReport` to
reviewable provenance: git revision and dirty state, matrix content hash and
version, prompt file versions/hashes, and only the audit event, record,
operation, and ledger IDs actually present in case evidence. Absent identifiers
are emitted as `null` or an empty list. The parent evaluation command can emit
this returned mapping as JSON alongside its normal report without exposing
secrets or invoking a second case runner.

The source of truth is [the executable manifest implementation](../backend/agentbank/evals/traceability.py),
with validation coverage in
[`backend/tests/test_traceability.py`](../backend/tests/test_traceability.py).

<a id="executable-manifest"></a>

The admin self-test endpoint and user-requested evaluation run now include this
artifact in their response. The artifact is generated from the same in-memory
EvaluationReport and does not launch a second evaluation pass. Its dirty flag
is true while the source tree contains uncommitted changes.
