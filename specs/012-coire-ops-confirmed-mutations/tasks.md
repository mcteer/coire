# Tasks: Coire-Ops with Confirmed Mutations

**Input**: Design documents from `specs/012-coire-ops-confirmed-mutations/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: Contract, unit, browser, composed integration, restart, concurrency, confinement, image,
and observability tests are required by the spec and constitution.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish the feature branch, service skeleton, configuration, and dependency evidence.

- [X] T001 Verify branch ancestry and record the Feature 012 Constitution Check in `specs/012-coire-ops-confirmed-mutations/plan.md`
- [X] T002 Create the ops contract module and exports in `packages/coire-core/src/coire_core/models/ops.py` and `packages/coire-core/src/coire_core/models/__init__.py`
- [X] T003 [P] Add ops confirmation settings and secret-file configuration in `packages/coire-core/src/coire_core/settings.py` and `deploy/compose/README.md`
- [X] T004 [P] Convert the existing ops entrypoint into a long-lived service skeleton in `apps/coire-agent/ops/coire_ops/app.py` and `apps/coire-agent/ops/coire_ops/__main__.py`
- [X] T005 Document every added dependency, exact pin, purpose, and licence in `specs/012-coire-ops-confirmed-mutations/review.md` and update the lockfile only if required

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Add strict shared state, authentication, persistence, and routing foundations required by every story.

- [X] T006 Add strict conversation, message, session, action-union, proposal, token, confirmation, decline, and response schemas in `packages/coire-core/src/coire_core/models/ops.py`
- [X] T007 [P] Add schema validation and irreversible-action exclusion tests in `packages/coire-core/tests/test_ops_models.py`
- [X] T008 Add ops session/conversation/message/proposal/token rows and constraints in `apps/coire-api/src/coire_api/db.py`
- [X] T009 Add a reversible Alembic migration for ops state in `apps/coire-api/alembic/versions/0012_ops_confirmations.py`
- [X] T010 [P] Add migration upgrade/downgrade coverage in `apps/coire-api/tests/unit/test_migrations.py`
- [X] T011 Add a dedicated ops-service principal and constant-time secret authentication in `apps/coire-api/src/coire_api/auth.py`
- [X] T012 Add internal ops session/proposal routes and public admin conversation/proposal routers in `apps/coire-api/src/coire_api/routes/internal_ops.py`, `apps/coire-api/src/coire_api/routes/admin_ops.py`, and `apps/coire-api/src/coire_api/app.py`
- [X] T013 [P] Add route authentication sweep and generated OpenAPI contract assertions in `apps/coire-api/tests/contract/test_admin_ops.py` and `apps/coire-api/tests/contract/test_auth_route_sweep.py`
- [X] T014 Regenerate `apps/coire-web/src/api/schema.d.ts` from the authoritative OpenAPI document

**Checkpoint**: All wire shapes, database constraints, service identity, and authenticated route shells exist.

---

## Phase 3: User Story 1 - Ask, Propose, Approve, Execute (Priority: P1) 🎯 MVP

**Goal**: An admin receives a fully resolved reversible proposal, explicitly approves or declines it, and sees an audited result.

**Independent Test**: Propose unload for a ready idle instance; verify no mutation before approval,
approve it, observe stopped state and an audit naming the human actor and ops proposer; repeat decline.

### Tests for User Story 1

- [ ] T015 [P] [US1] Add proposal/create/confirm/decline contract tests in `apps/coire-api/tests/contract/test_admin_ops.py`
- [X] T016 [P] [US1] Add exact resolved-action registry unit tests for unload, kill, pin, unpin, and load in `apps/coire-api/tests/unit/test_ops_actions.py`
- [ ] T017 [P] [US1] Add Ask Coire confirmation-card browser tests in `apps/coire-web/src/pages/admin/AskCoire.test.tsx`
- [ ] T018 [US1] Add the normal unload approval and decline composed integration scenario in `tests/integration/test_ops_confirmations.py`

### Implementation for User Story 1

- [X] T019 [US1] Implement canonical action digesting and proposal/token minting in `apps/coire-api/src/coire_api/ops_tokens.py`
- [X] T020 [US1] Implement conversation/message/proposal persistence and projection in `apps/coire-api/src/coire_api/ops.py`
- [X] T021 [US1] Implement the fixed reversible action registry using existing domain services in `apps/coire-api/src/coire_api/ops_actions.py`
- [X] T022 [US1] Implement proposal, confirmation, decline, and status endpoints in `apps/coire-api/src/coire_api/routes/admin_ops.py` and `apps/coire-api/src/coire_api/routes/internal_ops.py`
- [X] T023 [US1] Extend the ops-only admin client with typed read/propose methods and no confirm method in `apps/coire-agent/ops/coire_ops/admin_client.py`
- [X] T024 [US1] Implement model-backed answer/proposal orchestration with shallow validated output in `apps/coire-agent/ops/coire_ops/service.py` and `apps/coire-agent/ops/coire_ops/model.py`
- [X] T025 [US1] Wire `coire-api` Ask Coire forwarding to the internal ops service in `apps/coire-api/src/coire_api/routes/admin_ops.py`
- [ ] T026 [US1] Render exact action details, expiry, approve, decline, pending, and terminal states in `apps/coire-web/src/pages/admin/AskCoire.tsx` and `apps/coire-web/src/components/OpsProposalCard.tsx`
- [ ] T027 [US1] Add API client methods for conversations, messages, approval, and decline in `apps/coire-web/src/api/client.ts`
- [ ] T028 [US1] Audit creation, decline, confirmation, dispatch, and terminal outcome with actor/proposer fields in `apps/coire-api/src/coire_api/ops.py` and `apps/coire-api/src/coire_api/ops_actions.py`

**Checkpoint**: The independently testable unload approval/decline journey works end to end.

---

## Phase 4: User Story 2 - Confirmation Cannot Be Redirected (Priority: P1)

**Goal**: Altered, replayed, expired, concurrent, non-admin, service-token, and stale confirmations fail closed.

**Independent Test**: Race two exact confirmations and attempt altered-target, replay, expiry,
service-token, non-admin, and stale-state variants; exactly one exact human confirmation executes.

### Tests for User Story 2

- [X] T029 [P] [US2] Add Argon2 token, canonical digest, expiry, and mismatch unit tests in `apps/coire-api/tests/unit/test_ops_tokens.py`
- [X] T030 [P] [US2] Add row-lock concurrency and stale-precondition tests in `apps/coire-api/tests/unit/test_ops_confirmation.py`
- [ ] T031 [P] [US2] Add non-admin, API-key, and ops-service confirmation refusal contract tests in `apps/coire-api/tests/contract/test_admin_ops.py`
- [ ] T032 [US2] Extend composed integration for concurrent single use, replay, expiry, redirection, and stale state in `tests/integration/test_ops_confirmations.py`

### Implementation for User Story 2

- [X] T033 [US2] Implement constant-time token parsing/verification and bounded refusal reasons in `apps/coire-api/src/coire_api/ops_tokens.py`
- [X] T034 [US2] Atomically lock, validate, consume, and transition a pending proposal before dispatch in `apps/coire-api/src/coire_api/ops.py`
- [X] T035 [US2] Enforce current resource version/state preconditions in every action handler in `apps/coire-api/src/coire_api/ops_actions.py`
- [X] T036 [US2] Map confirmation failures to RFC 9457 problem details without leaking tokens or model output in `apps/coire-api/src/coire_api/routes/admin_ops.py`
- [ ] T037 [US2] Add bounded confirmation-refusal metrics and structured proposal identifiers in `apps/coire-api/src/coire_api/ops.py`

**Checkpoint**: No altered or repeated authority can execute, and concurrent redemption is exactly once.

---

## Phase 5: User Story 3 - Model-Unreachable Degraded Mode (Priority: P2)

**Goal**: Status remains factual and read-only without inference, mutation requests are explicitly refused, and full capability resumes automatically.

**Independent Test**: Make the pinned admin model unavailable, ask status and an action, verify one
degraded snapshot answer and one proposal refusal with zero model calls; restore the model and propose without restarting services.

### Tests for User Story 3

- [X] T038 [P] [US3] Add deterministic degraded status/action classification tests in `apps/coire-api/tests/unit/test_ops_degraded.py`
- [X] T039 [P] [US3] Add ops service model-health transition tests in `apps/coire-agent/tests/test_ops_service.py`
- [ ] T040 [P] [US3] Add degraded banner and disabled approval UI tests in `apps/coire-web/src/pages/admin/AskCoire.test.tsx`
- [ ] T041 [US3] Extend composed integration for unavailable/recovered admin model with zero core inference in `tests/integration/test_ops_confirmations.py`

### Implementation for User Story 3

- [X] T042 [US3] Extend the existing bounded snapshot responder to distinguish status questions from action requests in `apps/coire-api/src/coire_api/console/ops.py`
- [X] T043 [US3] Add pinned-model readiness probing and automatic healthy/degraded transitions in `apps/coire-agent/ops/coire_ops/service.py`
- [X] T044 [US3] Implement no-model degraded responses and proposal suppression in `apps/coire-api/src/coire_api/routes/admin_ops.py`
- [ ] T045 [US3] Surface degraded status, source facts, and recovery in `apps/coire-web/src/pages/admin/AskCoire.tsx`

**Checkpoint**: Ops remains useful and mutation-free while its Studio model is unavailable.

---

## Phase 6: User Story 4 - Irreversible Actions Stay Absent (Priority: P2)

**Goal**: The model, service, API, image, and UI cannot represent or invoke irreversible/admin-general operations.

**Independent Test**: Ask to retire a model and delete a user, enumerate schemas/tools, and inspect
the image; no proposal exists and no shell, git, filesystem, Docker, acquisition, or user mutation capability is present.

### Tests for User Story 4

- [X] T046 [P] [US4] Add tool enumeration and malformed/irreversible proposal tests in `apps/coire-agent/tests/test_ops_service.py`
- [ ] T047 [P] [US4] Add ops image boundary and package-content tests in `apps/coire-agent/tests/test_image_boundary.py`
- [ ] T048 [P] [US4] Add compose topology tests proving core-only ops placement and no Docker/Studio networks in `tests/integration/test_topology.py`
- [ ] T049 [US4] Extend composed integration with retire/delete-user refusal and zero audit mutation in `tests/integration/test_ops_confirmations.py`

### Implementation for User Story 4

- [X] T050 [US4] Restrict the Pydantic AI ops toolset to bounded read/propose tools in `apps/coire-agent/ops/coire_ops/model.py`
- [ ] T051 [US4] Harden the ops image to contain only its runtime/client and no user harness or debug tools in `apps/coire-agent/ops.Dockerfile`
- [ ] T052 [US4] Add the isolated, resource-limited, secret-backed `coire-ops` service and healthcheck in `deploy/compose/compose.yaml`
- [ ] T053 [US4] Restrict `coire-ops` to internal API and telemetry networks and keep it off the Docker proxy/edge/database networks in `deploy/compose/compose.yaml`
- [ ] T054 [US4] Add ops service secret provisioning and digest pin enforcement in `deploy/compose/coire-up`, `deploy/compose/.env.example`, and `scripts/pin-images.sh`

**Checkpoint**: Irreversible authority and general-purpose tooling are absent by construction.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Complete restart invalidation, observability, operations, generated artifacts, and all release gates.

- [ ] T055 Add session registration/heartbeat and prior-session proposal invalidation in `apps/coire-api/src/coire_api/ops.py` and `apps/coire-agent/ops/coire_ops/service.py`
- [ ] T056 [P] Add ops-container restart invalidation integration coverage in `tests/integration/test_ops_confirmations.py`
- [ ] T057 [P] Add spans, bounded metrics, and structured logs across API and service paths in `apps/coire-api/src/coire_api/ops.py` and `apps/coire-agent/ops/coire_ops/service.py`
- [ ] T058 [P] Add ops proposal/degraded panels and alerts in `deploy/observability/grafana/provisioning/dashboards/coire-runs.json` and `deploy/observability/prometheus/rules/coire-ops.yml`
- [ ] T059 [P] Add observe, kill, restart, secret rotation, rollback, and stale-proposal procedures in `docs/runbooks/coire-ops.md`
- [ ] T060 Update environment documentation and architecture references in `deploy/compose/README.md`, `docs/ARCHITECTURE.md`, and `docs/ROADMAP.md`
- [ ] T061 Regenerate OpenAPI and `apps/coire-web/src/api/schema.d.ts`; add freshness assertions in `apps/coire-api/tests/contract/test_admin_ops.py`
- [ ] T062 Run migration `upgrade head → downgrade -1 → upgrade head`, full Python/web/OpenAPI suites, and record evidence in `specs/012-coire-ops-confirmed-mutations/review.md`
- [ ] T063 Build `coire-agent-ops`, run image policy and critical-CVE scans, generate an SPDX SBOM, and record licence evidence in `specs/012-coire-ops-confirmed-mutations/review.md`
- [ ] T064 Run `quickstart.md` composed confirmation/restart/degraded/isolation scenarios and record non-secret evidence in `specs/012-coire-ops-confirmed-mutations/review.md`
- [ ] T065 Perform a non-destructive convergence analysis and append/complete every remaining task in `specs/012-coire-ops-confirmed-mutations/tasks.md`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup** has no dependencies.
- **Foundational** depends on Setup and blocks all user stories.
- **US1** is the MVP and depends on Foundational.
- **US2** depends on US1 proposal/token/dispatch paths.
- **US3** depends only on Foundational service routing and can proceed alongside US1 after route shells exist.
- **US4** depends only on Foundational contracts and can proceed alongside US1/US3.
- **Polish** depends on every selected story.

### Parallel Opportunities

- T003/T004 and T007/T010/T013 operate in separate files.
- Tests within each story can be authored in parallel before implementation.
- US3 degraded handling and US4 image/tool confinement can proceed in parallel with US1 API/UI work.
- T056–T059 cover independent restart, telemetry, dashboard, and documentation surfaces.

## Parallel Example: User Story 1

```text
T015 Contract tests: apps/coire-api/tests/contract/test_admin_ops.py
T016 Action tests: apps/coire-api/tests/unit/test_ops_actions.py
T017 Browser tests: apps/coire-web/src/pages/admin/AskCoire.test.tsx
```

## Implementation Strategy

### MVP First

1. Complete Setup and Foundational phases.
2. Complete US1 with unload approval and decline only through the generic fixed registry.
3. Validate no mutation before approval and correct dual-identity audit.

### Incremental Delivery

1. Add US2 security/replay/concurrency enforcement.
2. Add US3 deterministic degraded behavior and automatic recovery.
3. Add US4 structural absence and hardened deployment.
4. Complete restart invalidation, observability, docs, generated artifacts, image gates, and convergence.

## Format Validation

All 65 tasks use the required checkbox, sequential task id, optional `[P]`, required user-story label
inside story phases, specific action, and concrete file path.
