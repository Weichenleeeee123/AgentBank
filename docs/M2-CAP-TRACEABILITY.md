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
