# Tasks: Model Instances and Cluster State

**Input**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

## Phase 1: Contracts and persistence

- [x] T001 Add strict instance/state/event/member/cluster-state contracts and enums in `packages/coire-core/src/coire_core/models/instance.py`
- [x] T002 Export feature 005 contracts from `packages/coire-core/src/coire_core/models/__init__.py`
- [x] T003 [P] Add contract validation tests in `packages/coire-core/tests/test_instance_models.py`
- [x] T004 Add instance, member, transition, registration-attempt ORM rows and node/engine fields in `apps/coire-api/src/coire_api/db.py`
- [x] T005 Create reversible `apps/coire-api/alembic/versions/0007_model_instances.py`, including legacy engine/reservation migration
- [x] T006 [P] Extend migration-chain tests in `apps/coire-api/tests/unit/test_migrations.py`

## Phase 2: Durable lifecycle foundation

- [x] T007 Add transition validation, atomic event append, and typed projection in `apps/coire-api/src/coire_api/instance/service.py`
- [x] T008 Add idempotent reservation/member/engine command preparation keyed by instance in `apps/coire-api/src/coire_scheduler/instances.py`
- [x] T009 Implement DBOS requested→ready lifecycle and restart-safe failure cleanup in `apps/coire-api/src/coire_scheduler/instances.py`
- [x] T010 Wire instance dispatch/recovery into `apps/coire-api/src/coire_scheduler/main.py`
- [x] T011 Adapt feature 004 placement execution to create and operate instance holders rather than model holders
- [x] T012 [P] Add exhaustive state-transition and idempotence unit tests in `apps/coire-api/tests/unit/test_instance_lifecycle.py`

## Phase 3: User Story 1 — restart-safe launch (P1)

- [x] T013 [US1] Record `instance_id` on engine commands and engine rows; enforce one live engine per member
- [x] T014 [US1] Reattach pending/running node commands and adopt matching engine identity after scheduler restart
- [x] T015 [US1] Fail and release reservations when a selected node/copy disappears
- [x] T016 [P] [US1] Add composed scheduler-restart launch scenario in `tests/integration/test_instance_lifecycle_integration.py`

## Phase 4: User Story 2 — create and await (P1)

- [x] T017 [P] [US2] Add create/list/get/drain API contract tests in `apps/coire-api/tests/contract/test_instances.py`
- [x] T018 [US2] Implement authenticated `POST/GET /api/v1/instances` and instance detail routes in `apps/coire-api/src/coire_api/routes/instances.py`
- [x] T019 [US2] Implement persisted SSE replay/current-state/terminal events with `Last-Event-ID`
- [x] T020 [US2] Implement single-flight create for concurrent cold requests
- [x] T021 [P] [US2] Add SSE late-subscriber/failure/stop tests

## Phase 5: User Story 3 — cluster state (P1)

- [x] T022 [P] [US3] Add cluster-state contract test and admin guard checks
- [x] T023 [US3] Persist GPU, health observation time and degraded/unreachable projections from node probes
- [x] T024 [US3] Implement typed `GET /api/v1/state` with nodes, ledgers, members and effective instance state
- [x] T025 [US3] Update reconciliation to adopt matched members, flag orphans, and make unreachable instances unavailable
- [x] T026 [P] [US3] Add projection/reconciliation unit and composed tests

## Phase 6: User Story 4 — multiple instances and drain (P2)

- [x] T027 [US4] Replace model-direct gateway loading/routing with ready-instance selection
- [x] T028 [US4] Implement affinity then least-in-flight routing with instance-scoped leases
- [x] T029 [US4] Implement bounded drain, forced termination and confirmed reservation release
- [x] T030 [US4] Change idle TTL and LRU eviction to drain instances before unload
- [x] T031 [P] [US4] Test coexistence, least-load routing, zero-failure handoff and drain timeout

## Phase 7: User Story 5 — declared nodes and one-time tokens (P2)

- [x] T032 [P] [US5] Add declare/issue/revoke and registration refusal contract tests
- [x] T033 [US5] Implement token digest/issue/revoke service without persisting plaintext
- [x] T034 [US5] Add admin declaration and token lifecycle routes
- [x] T035 [US5] Require a declared row and valid unused token in registration; consume atomically
- [x] T036 [US5] Audit successful, unknown, invalid, consumed and revoked attempts with token redaction
- [x] T037 [US5] Remove self-registration/shared-token compatibility and prove no discovery mechanism exists
- [x] T038 [P] [US5] Add composed registration attack and token-reuse tests

## Phase 8: Observability, docs and release gates

- [x] T039 Add `coire.instance.*` spans, structured fields, lifecycle/drain/registration metrics
- [x] T040 Add Grafana instance/cluster panels and stalled-instance/node-registration alert rules
- [x] T041 Update OpenAPI and generated TypeScript schema
- [x] T042 Document configuration and operations in compose README and `docs/runbooks/instances.md`
- [x] T043 Run ruff, strict mypy, unit/contract tests, web tests/lint and OpenAPI freshness
- [x] T044 Run full composed integration including restart, engine death, drain, routing and registration abuse
- [x] T045 Build changed images; pass image policy and CRITICAL vulnerability scans
- [ ] T046 Execute `quickstart.md` with a <=1 GB model on the three-node cluster and record evidence
- [x] T047 Complete the PR description with spec link and Principles I–VII compliance

## Dependencies

T001→T004→T005→T007→T008→T009. US1/US2/US3 build on lifecycle foundation. US4 requires US2 and
US3 routing projections. US5 shares persistence but is otherwise independent. T039–T047 follow all
stories. Tests marked `[P]` may be authored alongside their implementation targets.
