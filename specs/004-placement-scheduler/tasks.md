# Tasks: Placement Scheduler and Auto-Unload

**Input**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: Required by Constitution Principle VII. Write story tests before implementation.

## Phase 1: Setup

- [x] T001 Add placement settings/default documentation in `packages/coire-core/src/coire_core/settings.py` and `deploy/compose/README.md`
- [x] T002 [P] Add placement dashboard/rule placeholders in `deploy/compose/grafana/provisioning/dashboards/coire-jobs.json` and `deploy/compose/prometheus/rules/coire-placement.yml`

## Phase 2: Foundational contracts and persistence

- [x] T003 Add strict ledger, reservation, lease, placement-decision, eviction, and refusal Pydantic contracts in `packages/coire-core/src/coire_core/models/placement.py`
- [x] T004 Export placement contracts from `packages/coire-core/src/coire_core/models/__init__.py`
- [x] T005 [P] Add placement contract invariant tests in `packages/coire-core/tests/test_placement_models.py`
- [x] T006 Add ledger, memory-reservation, request-lease, decision, and eviction SQLAlchemy rows in `apps/coire-api/src/coire_api/db.py`
- [x] T007 Add reversible migration in `apps/coire-api/alembic/versions/0006_memory_ledger.py` with sandbox seed rows for declared nodes
- [x] T008 [P] Add migration upgrade/downgrade tests in `apps/coire-api/tests/unit/test_migrations.py`
- [x] T009 Implement per-node transaction advisory lock and ledger projection primitives in `apps/coire-api/src/coire_scheduler/placement.py`

## Phase 3: User Story 1 — Correct LRU eviction (P1)

**Independent test**: Artificially fill a node, skip a busy LRU entry, evict the next eligible entry, and admit exactly one competing load.

- [x] T010 [P] [US1] Add exact-fit, LRU, busy-skip, pinned-refusal, unload-failure, and concurrent-admission unit tests in `apps/coire-api/tests/unit/test_placement_policy.py`
- [x] T011 [P] [US1] Add typed/admin-guarded placement API contract tests in `apps/coire-api/tests/contract/test_admin_ledger.py`
- [x] T012 [US1] Implement deterministic candidate ranking and typed occupant reasons in `apps/coire-api/src/coire_scheduler/placement.py`
- [x] T013 [US1] Implement durable DBOS select/evict/reserve/load workflow in `apps/coire-api/src/coire_scheduler/placement.py`
- [x] T014 [US1] Generalize the API command executor for authenticated engine load/unload handoff in `apps/coire-api/src/coire_api/placement/executor.py`
- [x] T015 [US1] Wire placement workflow dispatch/recovery into `apps/coire-api/src/coire_scheduler/main.py`
- [x] T016 [US1] Implement placement submit/status routes with admin guard and audit in `apps/coire-api/src/coire_api/routes/admin_ledger.py`
- [x] T017 [US1] Register placement routes/executor lifecycle in `apps/coire-api/src/coire_api/app.py`
- [x] T018 [US1] Add composed LRU and concurrent-admission coverage in `tests/integration/test_placement_scheduler.py`

## Phase 4: User Story 2 — Pin immunity (P1)

**Independent test**: A pinned model survives memory pressure and TTL; unpin makes it immediately eligible.

- [x] T019 [P] [US2] Add pin/unpin contract and audit tests in `apps/coire-api/tests/contract/test_admin_ledger.py`
- [x] T020 [P] [US2] Add pin-immunity policy tests in `apps/coire-api/tests/unit/test_placement_policy.py`
- [x] T021 [US2] Implement pin mutation and explicit pinned-node precedence in `apps/coire-api/src/coire_api/placement/service.py`
- [x] T022 [US2] Add pin/unpin admin routes in `apps/coire-api/src/coire_api/routes/admin_ledger.py`
- [x] T023 [US2] Add composed pinned pressure coverage in `tests/integration/test_placement_scheduler.py`

## Phase 5: User Story 3 — Idle TTL (P2)

**Independent test**: Per-model TTL unloads idle unpinned engines, while active leases and pinned engines remain.

- [x] T024 [P] [US3] Add request lease, stale-lease, per-model TTL, and race tests in `apps/coire-api/tests/unit/test_idle_ttl.py`
- [x] T025 [P] [US3] Add gateway last-used/in-flight tests in `apps/coire-api/tests/unit/test_gateway_proxy.py`
- [x] T026 [US3] Implement gateway request lease acquire/refresh/release around proxying in `apps/coire-api/src/coire_api/gateway/proxy.py`
- [x] T027 [US3] Implement DBOS idle control loop and confirmed-unload release ordering in `apps/coire-api/src/coire_scheduler/placement.py`
- [x] T028 [US3] Wire periodic TTL dispatch into `apps/coire-api/src/coire_scheduler/main.py`
- [x] T029 [US3] Add composed traffic/TTL/restart coverage in `tests/integration/test_placement_scheduler.py`

## Phase 6: User Story 4 — Trustworthy ledger (P2)

**Independent test**: Both node projections reconcile budget, sandbox, model reservations, free bytes, measured drift, and freshness.

- [x] T030 [P] [US4] Add ledger projection, budget reduction, unhealthy/stale node, and drift tests in `apps/coire-api/tests/unit/test_ledger.py`
- [x] T031 [P] [US4] Add ledger list/update contract tests in `apps/coire-api/tests/contract/test_admin_ledger.py`
- [x] T032 [US4] Implement health ingestion, conservative unreachable accounting, and drift projection in `apps/coire-api/src/coire_api/placement/service.py`
- [x] T033 [US4] Implement ledger list/update endpoints with audit rows in `apps/coire-api/src/coire_api/routes/admin_ledger.py`
- [x] T034 [US4] Emit bounded admission, reservation, eviction, TTL, queue, and drift metrics/spans in `apps/coire-api/src/coire_scheduler/placement.py`
- [x] T035 [US4] Add drift and refused-capacity alerts in `deploy/compose/prometheus/rules/coire-placement.yml`
- [x] T036 [US4] Add budget/reservation/drift panels and trace links in `deploy/compose/grafana/provisioning/dashboards/coire-jobs.json`

## Phase 7: Contracts, documentation, and release gates

- [x] T037 Regenerate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`
- [x] T038 [P] Document inspect, pin, evict, TTL, kill, and rollback operations in `docs/runbooks/placement.md`
- [x] T039 [P] Document all placement environment variables in `deploy/compose/README.md`
- [x] T040 Run Ruff, strict mypy, non-integration pytest, web tests/lint, OpenAPI freshness, and contract validation
- [x] T041 Build changed production images and pass image policy plus CRITICAL CVE scans
- [x] T042 Run all composed placement scenarios in `tests/integration/test_placement_scheduler.py`
- [ ] T043 Run the ≤1 GB placement/eviction engine test on one Studio and record no-swap evidence
- [ ] T044 Execute `quickstart.md` on the three-node cluster and create `specs/004-placement-scheduler/execution-record.md`
- [x] T045 Complete the PR description with spec link and Constitution Principles I–VII compliance

## Dependencies and execution order

- Setup precedes foundational work; foundational persistence/contracts block every story.
- US1 establishes admission/eviction. US2 adds pin rules. US3 adds leases/TTL. US4 projects and observes the shared ledger.
- Within every story: tests first, contracts/models before services, services before routes/integration.
- T037–T045 follow all stories.

## Parallel opportunities

- T001/T002, T005/T008, story test tasks, and documentation/dashboard work touch separate files.
- Policy pure functions can be tested independently of route contracts after T003–T009.

## Implementation strategy

Ship US1 as the MVP after foundational work, then pin immunity, TTL leases, and operator projection.
Keep all changes additive and preserve the existing direct admin engine verbs for rollback until the
next feature consumes placement decisions exclusively.

## Phase 8: Convergence

- [x] T046 Integrate acquisition conversion holds into the authoritative memory ledger per FR-001 and the conversion-reservation assumption (partial)
- [x] T047 Preserve per-node health and stale-sample reasons in placement refusals per FR-018a/FR-018b (partial)
- [x] T048 Consume CPU and thermal saturation when ranking `single:auto` candidates per FR-018c (missing)
- [x] T049 Finalize eviction and TTL event outcomes with durable reasons per FR-020 (partial)
- [x] T050 Add placement trace navigation to the Grafana dashboard per Constitution VI and T036 (partial)
