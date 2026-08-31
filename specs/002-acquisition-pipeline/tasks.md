# Tasks: Model Acquisition Pipeline

**Input**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: Required by the specification and Constitution Principle VII. Tests are written before
the corresponding implementation and must demonstrably fail first.

## Phase 1: Setup

- [X] T001 Pin `dbos==2.24.0` in `apps/coire-api/pyproject.toml`, update `uv.lock`, and document its MIT licence and durable-workflow purpose in `specs/002-acquisition-pipeline/research.md`
- [ ] T002 [P] Add acquisition settings and documented defaults to `packages/coire-core/src/coire_core/settings.py` and `deploy/compose/README.md`
- [ ] T003 [P] Add acquisition Prometheus rule file wiring and dashboard provisioning placeholders in `deploy/compose/compose.yaml`

## Phase 2: Foundational contracts and persistence

**Purpose**: Shared types and durable state that block every story.

- [X] T004 Add strict acquisition, variant, inspection, validation, reservation, and stage Pydantic wire models in `packages/coire-core/src/coire_core/models/acquisition.py`
- [X] T005 Extend node job enums and request/result contracts for pull, convert, validate, replicate, and cleanup in `packages/coire-core/src/coire_core/models/jobs.py`
- [X] T006 Export new public contracts from `packages/coire-core/src/coire_core/models/__init__.py`
- [X] T007 [P] Add core-model serialization and invariant tests in `packages/coire-core/tests/test_acquisition_models.py`
- [X] T008 Add SQLAlchemy rows for variants, workflows, stages, inspection, validation, variant copies, and reservations in `apps/coire-api/src/coire_api/db.py`
- [X] T009 Add reversible additive migration and existing-model/default-variant seeding in `apps/coire-api/alembic/versions/0005_acquisition_variants.py`
- [X] T010 [P] Add upgrade/downgrade and legacy-row migration tests in `apps/coire-api/tests/unit/test_migrations.py`
- [X] T011 Add DBOS runtime initialization and recovery ownership in `apps/coire-api/src/coire_scheduler/dbos_runtime.py`
- [X] T012 Wire scheduler lifespan/readiness to DBOS in `apps/coire-api/src/coire_scheduler/main.py`
- [X] T013 [P] Add DBOS configuration/recovery tests in `apps/coire-api/tests/unit/test_dbos_runtime.py`

**Checkpoint**: Contracts, schema, and durable runtime are ready.

## Phase 3: User Story 1 — Raw repository reaches ready (P1)

**Goal**: Inspect, pull once, convert, validate, replicate, clean raw data, and expose stage progress.

**Independent test**: A ≤1 GB raw safetensors fixture reaches two verified copies and a validated
ready variant through the complete workflow.

### Tests

- [ ] T014 [P] [US1] Add admin acquisition and workflow-status contract tests in `apps/coire-api/tests/contract/test_admin_acquisitions.py`
- [ ] T015 [P] [US1] Add node reservation and acquisition-job contract tests in `apps/coire-node/tests/contract/test_node_acquisition.py`
- [ ] T016 [P] [US1] Add conversion argv allowlist, partial cleanup, and disk-failure unit tests in `apps/coire-node/tests/unit/test_conversion.py`
- [ ] T017 [P] [US1] Add smoke, perplexity, and tool-template validation unit tests in `apps/coire-node/tests/unit/test_validation.py`
- [ ] T018 [US1] Add raw and already-MLX end-to-end composed tests in `tests/integration/test_acquisition_pipeline.py`

### Implementation

- [ ] T019 [US1] Implement metadata-only Hub inspection and immutable revision resolution in `apps/coire-api/src/coire_api/registry/inspection.py`
- [ ] T020 [US1] Implement acquisition persistence, deduplication, and stage projections in `apps/coire-api/src/coire_api/registry/acquisition.py`
- [ ] T021 [US1] Implement admin submit/status/retry routes with admin guards and audit rows in `apps/coire-api/src/coire_api/routes/admin_acquisitions.py`
- [ ] T022 [US1] Register acquisition routes in `apps/coire-api/src/coire_api/app.py`
- [ ] T023 [US1] Extend the authenticated node client for reservation and acquisition job verbs in `apps/coire-api/src/coire_api/nodes_client.py`
- [ ] T024 [US1] Implement durable inspect/pull/convert/validate/replicate DBOS workflow and idempotent steps in `apps/coire-api/src/coire_scheduler/acquisition.py`
- [ ] T025 [US1] Wire scheduler startup recovery and workflow execution in `apps/coire-api/src/coire_scheduler/main.py`
- [ ] T026 [US1] Implement persistent node reservation ledger with memory/disk admission and idempotent release in `apps/coire-node/src/coire_node/reservations.py`
- [ ] T027 [US1] Implement explicit-argv `mlx_lm.convert` supervision, reservation-before-PID ordering, atomic publication, and partial cleanup in `apps/coire-node/src/coire_node/conversion.py`
- [ ] T028 [US1] Implement deterministic smoke/perplexity/tool-template checks in `apps/coire-node/src/coire_node/validation.py`
- [ ] T029 [US1] Extend node job persistence and worker dispatch for acquisition operations in `apps/coire-node/src/coire_node/jobs.py` and `apps/coire-node/src/coire_node/worker.py`
- [ ] T030 [US1] Implement authenticated node reservation/job routes in `apps/coire-node/src/coire_node/routes/jobs.py`
- [ ] T031 [US1] Wire new node dependencies and startup recovery in `apps/coire-node/src/coire_node/deps.py` and `apps/coire-node/src/coire_node/agent.py`
- [ ] T032 [US1] Implement cleanup only after validation and two-copy verification in `apps/coire-api/src/coire_scheduler/acquisition.py`
- [ ] T033 [US1] Project the published default variant through existing registry and `/v1/models` resolution in `apps/coire-api/src/coire_api/registry/service.py`

**Checkpoint**: US1 is independently functional and tested.

## Phase 4: User Story 2 — Early actionable refusal (P1)

**Goal**: Reject unsupported, GGUF-only, no-fit, and unlicensed gated repositories before weights move.

**Independent test**: Every refusal has a stable problem code and zero transferred weight bytes.

### Tests

- [ ] T034 [P] [US2] Add inspection tests for raw, MLX, GGUF, unsupported, gated, and oversized metadata fixtures in `apps/coire-api/tests/unit/test_inspection.py`
- [ ] T035 [P] [US2] Add specific RFC 9457 refusal contract tests and admin-auth coverage in `apps/coire-api/tests/contract/test_admin_acquisitions.py`
- [ ] T036 [US2] Add zero-transfer rejection scenarios to `tests/integration/test_acquisition_pipeline.py`

### Implementation

- [ ] T037 [US2] Implement architecture/format/gating detection and GGUF source guidance in `apps/coire-api/src/coire_api/registry/inspection.py`
- [ ] T038 [US2] Implement candidate-precision sizing and per-placement fit decisions in `apps/coire-api/src/coire_api/registry/inspection.py`
- [ ] T039 [US2] Map inspection failures to specific safe Coire problem types without local paths or credentials in `apps/coire-api/src/coire_api/routes/admin_acquisitions.py`
- [ ] T040 [US2] Enforce impossible-versus-currently-busy placement semantics in `apps/coire-api/src/coire_scheduler/acquisition.py`
- [ ] T041 [US2] Add a structural test that no Hub upload/publish operation or route exists in `apps/coire-api/tests/unit/test_no_hub_upload.py`

**Checkpoint**: US2 independently refuses bad inputs without transfer.

## Phase 5: User Story 3 — Compare and publish variants (P2)

**Goal**: Produce multiple variants without an implicit re-pull, compare results, and safely choose publication/default state.

**Independent test**: Two recipes share a base identity, show separate measurements, and only published variants resolve for users.

### Tests

- [ ] T042 [P] [US3] Add variant create/list/publication/default contract tests in `apps/coire-api/tests/contract/test_admin_variants.py`
- [ ] T043 [P] [US3] Add variant publication invariant and default-resolution tests in `apps/coire-api/tests/unit/test_variants.py`
- [ ] T044 [US3] Add kept-raw second-variant/no-WAN-pull integration coverage in `tests/integration/test_acquisition_pipeline.py`

### Implementation

- [ ] T045 [US3] Implement create/list/update variant service methods and comparison projection in `apps/coire-api/src/coire_api/registry/variants.py`
- [ ] T046 [US3] Implement admin variant routes with publication validation and audit rows in `apps/coire-api/src/coire_api/routes/admin_variants.py`
- [ ] T047 [US3] Register variant routes in `apps/coire-api/src/coire_api/app.py`
- [ ] T048 [US3] Start later variants at convert from retained raw data, or explicit verified dequantisation, in `apps/coire-api/src/coire_scheduler/acquisition.py`
- [ ] T049 [US3] Enforce one published default and user visibility rules in `apps/coire-api/src/coire_api/registry/service.py`
- [ ] T050 [US3] Regenerate API OpenAPI and TypeScript types in `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`

**Checkpoint**: US3 independently supports measured publication decisions.

## Phase 6: User Story 4 — Interruption recovery (P2)

**Goal**: Recover scheduler/node restarts from the last completed stage without duplicate work or published partials.

**Independent test**: Forced restart during pull/convert plus retry after validation failure completes with immutable earlier stage results.

### Tests

- [ ] T051 [P] [US4] Add DBOS completed-step reuse, duplicate attachment, and retry-boundary tests in `apps/coire-api/tests/unit/test_acquisition_workflow.py`
- [ ] T052 [P] [US4] Add node restart/partial-output recovery tests in `apps/coire-node/tests/unit/test_acquisition_recovery.py`
- [ ] T053 [US4] Add scheduler/node interruption scenarios to `tests/integration/test_acquisition_pipeline.py`

### Implementation

- [ ] T054 [US4] Implement deterministic workflow/idempotency keys and attach-on-duplicate behavior in `apps/coire-api/src/coire_scheduler/acquisition.py`
- [ ] T055 [US4] Resume from immutable successful stage results and retry only the earliest incomplete stage in `apps/coire-api/src/coire_scheduler/acquisition.py`
- [ ] T056 [US4] Recover or fail active node jobs, clean partial conversion output, and release orphan reservations in `apps/coire-node/src/coire_node/worker.py`
- [ ] T057 [US4] Persist resumable pull cursors and checksum progress without exposing paths in `apps/coire-node/src/coire_node/hub.py`

**Checkpoint**: All four stories are independently functional.

## Phase 7: Observability, security, documentation, and release gates

- [ ] T058 Add `coire.scheduler.acquisition.*` and `coire.node.acquisition.*` spans and structured identifiers across scheduler/API/node paths
- [ ] T059 Add bounded-cardinality duration, byte, queue, reservation, validation, and estimate-drift metrics in `apps/coire-api/src/coire_scheduler/acquisition.py` and `apps/coire-node/src/coire_node/metrics.py`
- [ ] T060 Add stuck-stage, conversion-failure, and >10% estimate-drift alerts in `deploy/compose/prometheus/rules/coire-acquisition.yml`
- [ ] T061 Add acquisition/validation panels and trace links in `deploy/compose/grafana/provisioning/dashboards/coire-jobs.json`
- [ ] T062 [P] Add acquisition operations, kill/retry, rollback, raw-retention, and failure diagnosis to `docs/runbooks/acquisition.md`
- [ ] T063 [P] Add all new environment variables and storage behavior to `deploy/compose/README.md`
- [ ] T064 Run Ruff formatting/lint, strict mypy, non-integration pytest, web tests/lint, OpenAPI freshness, and contract validation
- [ ] T065 Build all changed production images and run configured image scans without suppressions
- [ ] T066 Run composed acquisition integration tests including rejection, duplicate, disk-full, and restart cases
- [ ] T067 Run the ≤1 GB engine conversion/validation integration test on one Studio with `$COIRE_TEST_MODEL`
- [ ] T068 Execute `quickstart.md` on the real three-node cluster, record workflow/audit/metrics evidence in `specs/002-acquisition-pipeline/execution-record.md`, and confirm no swap or control-Wi-Fi data transfer
- [ ] T069 Complete the PR template with spec link, dependency licence rationale, and Constitution Principles I–VII compliance

## Dependencies and execution order

- Phase 1 precedes Phase 2; Phase 2 blocks all stories.
- US1 establishes the happy-path workflow used by US2–US4.
- US2 may begin after T024 and T019; US3 requires US1 variant persistence; US4 requires the full workflow.
- Observability and documentation land with their code, but final gate tasks run after all stories.
- Within each story: tests first and failing, then contracts/models, services, routes, integration, and validation.

## Parallel opportunities

- Tasks marked `[P]` touch separate files and can be prepared concurrently after their phase dependencies.
- Core contract tests, API contract tests, and node unit tests can be authored independently.
- Documentation and dashboards can be prepared while final integration tests execute.
