# M4 Release Closure Implementation Plan

> **For agentic workers:** Execute independent domains using dispatching-parallel-agents. Keep file ownership disjoint, review all changes, and never delete any file. Existing PLAN and readiness audit are the user-approved requirements; the user authorized closing all findings and producing a freezeable candidate.

**Goal:** Resolve every known readiness finding, verify business/security/user journeys, and produce reproducible release evidence and submission materials.

**Architecture:** Preserve the single deterministic business kernel. Give new user messages distinct request IDs while preserving idempotent retries, keep channel identity separate from event IDs, and expose a server-side signed simulated IM gateway. Frontend rendering consumes authoritative structured results. Staff edits remain drafts/retry requests and cannot authorize money. Offline PWA caches only the static shell. Release checks record actual results and external blockers without inventing evidence.

**Tech Stack:** Python/FastAPI/SQLAlchemy, native HTML/JS, pytest, Playwright, Docker Compose, existing OpenAI-compatible gateway.

## 1. Conversation correctness and IM gateway (root)

- [ ] Add failing regressions for repeated fresh queries, same-message retry, repeated identical new writes, cross-user message IDs, IM multi-turn and group isolation.
- [ ] Introduce persistent message request/result records with payload hash; serialize the message operation and preserve request IDs for network retries.
- [ ] Use stable user/channel/chat identity rather than event ID. Add browser gateway login/binding/message/callback endpoints; browser never receives signing secret and cannot choose bank identity in message payload.
- [ ] Provide current-user cards endpoint and plan/record deep-link resolution; review red operations and OTP stay App-only.
- [ ] Run focused agent/channel/authorization tests and inspect negative-path evidence.

## 2. Product frontend and PWA (frontend worker)

- [ ] Reproduce amount/status rendering and PIN continuation failures before fixing.
- [ ] Fix authoritative card rendering, Chinese uppercase amounts, fixed statuses, current-user card selection, confirmation/MFA and cooling actions; avoid raw JSON in ordinary flows.
- [ ] Add usable simulated IM page with separate identity, stable chat, private/group mode, explicit confirmation and App links. Use only server gateway APIs.
- [ ] Add icons, static-only service worker and offline explanation without caching API/account data.
- [ ] Add/run meaningful browser journeys and JS syntax checks.

## 3. Staff recovery and observability (staff worker)

- [ ] Add failing API tests for staff authorization, draft updates, safe retry requests, audit/DLP and metrics.
- [ ] Implement new staff router/service; retain existing routes and add draft/retry endpoints that never execute or approve bank writes.
- [ ] Add dashboard with observed request rate, model failure rate, breaker states and persisted hourly budget; label measured windows correctly.
- [ ] Extend staff page controls and run focused tests.

## 4. Reproducible release (release worker)

- [ ] Verify/repair Docker settings, independent secrets and device/staff setup, public/eval boundaries, health checks; add dependency lock and clean-environment script.
- [ ] Attempt Docker actual build/start/health safely; never delete containers/files/volumes. If daemon cannot start, report exact environment error and provide verified alternative runtime evidence.
- [ ] Create release verification/package commands that fail on missing evidence and never package credentials, local databases or caches.
- [ ] Verify fresh installation and deterministic runtime paths and record results.

## 5. Integration, audit, materials and freeze (root)

- [ ] Review each independent change against requirements and security boundaries, fix all review findings.
- [ ] Run full pytest, Ruff, Mypy, JS checks, 227-case evaluation and HTTP self-test; run browser App/IM/PIN/MFA/plan/recovery journeys and real-provider checks if credentials exist.
- [ ] Recheck previously untested business endpoints for authorization, stale state, error handling, duplicate side effects and truthful responses.
- [ ] Reconcile PLAN/STATUS/coverage/README; create release notes, architecture/security/algorithm submission docs, <=200-character introduction and evidence manifest.
- [ ] Prepare PPT/video assets using appropriate authoring skills if needed for requested candidate; distinguish artifacts ready from external upload/receipt.
- [ ] Record immutable candidate source hashes and exact checks, remaining external dependencies and submission checklist. Freeze only when mandatory gates actually pass.
