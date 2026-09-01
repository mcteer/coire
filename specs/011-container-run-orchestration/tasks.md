# Tasks: Container Run Orchestration on the Studios

**Input**: Design documents from `specs/011-container-run-orchestration/`

**Tests**: Constitution VII and the feature specification require contract, unit, integration, image,
recovery, confinement, and manual real-cluster tests. Test tasks precede their implementations.

## Phase 1: Setup

- [X] T001 Verify existing ignore files cover Python, Node, Docker, secrets, workspaces, results, and runtime artifacts in `.gitignore`, `.dockerignore`, and `apps/coire-web/eslint.config.js`
- [X] T002 Document the existing httpx, DBOS, Argon2, Docker Engine, and OrbStack dependency/licence decision in `specs/011-container-run-orchestration/research.md`
- [ ] T003 [P] Add run-orchestration settings with safe bounds and documented defaults in `packages/coire-core/src/coire_core/settings.py` and `deploy/compose/README.md`

## Phase 2: Foundational Contracts and Persistence

- [X] T004 Write strict run lifecycle, limits, result, token-scope, resource-usage, and problem contract tests in `packages/coire-core/tests/test_run_models.py`
- [X] T005 Implement run lifecycle, limits, result, token-scope, resource-usage, and node command models in `packages/coire-core/src/coire_core/models/runs.py` and export them from `coire_core.models`
- [X] T006 Write migration upgrade/downgrade tests for run, transition, token, and command state in `apps/coire-api/tests/unit/test_run_migration.py`
- [X] T007 Add reversible run, transition, token, and command tables with constraints and indexes in `apps/coire-api/alembic/versions/*_container_runs.py`
- [X] T008 Add typed SQLAlchemy rows and strict serialization helpers in `apps/coire-api/src/coire_api/db.py` and `apps/coire-api/src/coire_api/runs.py`
- [ ] T009 [P] Extend stable RFC 9457 run error codes in `packages/coire-core/src/coire_core/errors.py` and their gateway mapping tests

## Phase 3: User Story 1 — Execute and Collect a Studio Run (P1) 🎯 MVP

**Goal**: create, start, stream, wait, collect, record, and remove one run on a Studio.

**Independent Test**: submit a deterministic harness run and observe a recorded result and no remaining container.

- [X] T010 [P] [US1] Write Docker Engine UDS client unit tests for create/start/logs/wait/archive/inspect/remove/network operations in `apps/coire-node/tests/unit/test_docker_api.py`
- [X] T011 [P] [US1] Write authenticated node run route contract tests including idempotency and strict payload rejection in `apps/coire-node/tests/contract/test_node_runs.py`
- [ ] T012 [P] [US1] Write control-plane run create/get/events contract tests and ownership checks in `apps/coire-api/tests/contract/test_runs.py`
- [X] T013 [US1] Implement the typed local Docker Engine UDS client without shell execution in `apps/coire-node/src/coire_node/docker_api.py`
- [X] T014 [US1] Implement allowlisted image/argv/env/workspace validation and idempotent run lifecycle service in `apps/coire-node/src/coire_node/runs.py`
- [X] T015 [US1] Implement authenticated node create/start/log/wait/result/remove/list routes in `apps/coire-node/src/coire_node/routes/runs.py` and register them in `apps/coire-node/src/coire_node/app.py`
- [ ] T016 [US1] Extend the control-plane node client with typed run verbs and bounded streaming in `apps/coire-api/src/coire_api/nodes_client.py`
- [ ] T017 [US1] Implement authenticated owner/admin run create/get/events routes and services in `apps/coire-api/src/coire_api/routes/runs.py`, `apps/coire-api/src/coire_api/runs.py`, and `apps/coire-api/src/coire_api/app.py`
- [ ] T018 [US1] Implement deterministic Studio placement and a DBOS create/start/log/wait/collect/remove workflow in `apps/coire-api/src/coire_scheduler/runs.py` and dispatch it from `apps/coire-api/src/coire_scheduler/main.py`
- [ ] T019 [US1] Record node, container, timings, exit status, resource usage, bounded logs, strict result, transitions, and terminal audit rows in `apps/coire-api/src/coire_scheduler/runs.py`

## Phase 4: User Story 2 — Prove Core Never Runs User Harnesses (P1)

**Goal**: make Studio-only placement and absence of core runtime access structural and testable.

**Independent Test**: run successfully with no user agent image on core and no scheduler Docker socket.

- [ ] T020 [P] [US2] Write placement tests that reject core/non-Studio nodes and never fall back when Studios are unavailable in `apps/coire-api/tests/unit/test_run_placement.py`
- [ ] T021 [P] [US2] Write Compose and image topology tests proving scheduler/API lack Docker sockets and core need not hold the user image in `tests/integration/test_run_core_isolation.py`
- [ ] T022 [US2] Restrict placement candidates to healthy registered Studio nodes with sandbox capacity in `apps/coire-api/src/coire_scheduler/runs.py`
- [ ] T023 [US2] Add explicit production-compose and image-policy guards against user-run services or Docker socket mounts on core in `deploy/compose/compose.yaml` and `scripts/check-image-policy.sh`

## Phase 5: User Story 3 — Confine Every Run (P1)

**Goal**: permit only the gateway path while enforcing runtime and resource isolation.

**Independent Test**: gateway probe succeeds while internet, database, node, and peer probes fail; inspect shows every hardening control.

- [X] T024 [P] [US3] Write exact hardened Docker create-payload tests in `apps/coire-node/tests/unit/test_run_hardening.py`
- [ ] T025 [P] [US3] Write relay allowlist, header filtering, request-limit, and credential non-logging tests in `apps/coire-node/tests/unit/test_run_relay.py`
- [ ] T026 [P] [US3] Write Docker integration confinement and memory/timeout tests in `tests/integration/test_container_runs.py`
- [ ] T027 [US3] Implement a minimal gateway-only relay with `/v1` destination and method allowlists in `apps/coire-node/src/coire_node/run_relay.py`
- [ ] T028 [US3] Create one internal network per run, attach only run and relay, publish no ports, and remove both attachments/network idempotently in `apps/coire-node/src/coire_node/runs.py`
- [ ] T029 [US3] Enforce non-root, read-only rootfs, all capabilities dropped, no-new-privileges, PID/memory/CPU limits, tmpfs scratch, fixed mounts, and no restart in `apps/coire-node/src/coire_node/runs.py`
- [ ] T030 [US3] Enforce wall-clock, log-byte, and result-byte ceilings with distinct terminal errors in `apps/coire-node/src/coire_node/runs.py` and `apps/coire-api/src/coire_scheduler/runs.py`
- [X] T031 [US3] Validate workspace references under the configured workspace root without caller paths, traversal, symlinks, or arbitrary mounts in `apps/coire-node/src/coire_node/runs.py`

## Phase 6: User Story 4 — Immediate Kill and Token Revocation (P1)

**Goal**: revoke credentials synchronously and stop responsive containers within five seconds.

**Independent Test**: kill a long run, immediately receive gateway denial, and observe terminal audit state.

- [ ] T032 [P] [US4] Write run-token mint/hash/scope/expiry/revocation/spend tests in `apps/coire-api/tests/unit/test_run_tokens.py`
- [ ] T033 [P] [US4] Write gateway run-token authorization contract tests for model/tool/spend scope and admin-route denial in `apps/coire-api/tests/contract/test_run_token_auth.py`
- [ ] T034 [P] [US4] Write admin kill contract and audit tests including unreachable-node behavior in `apps/coire-api/tests/contract/test_admin_runs.py`
- [ ] T035 [US4] Implement opaque run-token minting, Argon2id verification, server-time expiry, atomic spend charge, and idempotent revocation in `apps/coire-api/src/coire_api/run_tokens.py`
- [ ] T036 [US4] Integrate run-token principals and scope checks into `/v1` authentication/resolution without granting other routes in `apps/coire-api/src/coire_api/identity.py` and `apps/coire-api/src/coire_api/gateway/resolution.py`
- [ ] T037 [US4] Implement admin list/kill routes that revoke before node contact and audit actor/time/outcome in `apps/coire-api/src/coire_api/routes/admin_runs.py`
- [ ] T038 [US4] Implement idempotent node kill/remove and a scheduler kill workflow with a five-second responsive-node deadline in `apps/coire-node/src/coire_node/runs.py` and `apps/coire-api/src/coire_scheduler/runs.py`

## Phase 7: User Story 5 — Restart Recovery and Orphan Reaping (P2)

**Goal**: recover exactly once and remove containers not backed by authoritative run rows.

**Independent Test**: restart scheduler mid-run and inject an orphan; one run completes and the orphan is reported/reaped.

- [ ] T039 [P] [US5] Write DBOS workflow replay/idempotency and dead-container recovery tests in `apps/coire-api/tests/unit/test_run_workflow.py`
- [ ] T040 [P] [US5] Write node observation and safe label-scoped orphan cleanup tests in `apps/coire-node/tests/unit/test_run_reconciler.py`
- [ ] T041 [P] [US5] Extend Docker integration coverage for scheduler restart, exact-once container identity, queued capacity, and orphan reaping in `tests/integration/test_container_runs.py`
- [ ] T042 [US5] Implement labeled-container observation and safe orphan reporting/reaping in `apps/coire-node/src/coire_node/run_reconciler.py`
- [ ] T043 [US5] Reconcile persisted run/container state before each retried DBOS effect and resume or terminally classify it in `apps/coire-api/src/coire_scheduler/runs.py`
- [ ] T044 [US5] Enforce configurable per-Studio slot caps, model-colocation preference, FIFO queueing, zero-sandbox ineligibility, and slot release in `apps/coire-api/src/coire_scheduler/runs.py`

## Phase 8: Polish and Cross-Cutting Gates

- [ ] T045 [P] Add `coire.node.run.*`, `coire.scheduler.run.*`, and `coire.api.run.*` spans, structured fields, bounded metrics, and content/token redaction across run paths
- [ ] T046 [P] Add run lifecycle/capacity/kill/recovery dashboard panels and actionable stuck-run/orphan/log-limit alerts in `deploy/observability/grafana/provisioning/dashboards/coire-runs.json` and `deploy/observability/prometheus/rules/coire-runs.yml`
- [ ] T047 [P] Add CLI run submit/list/show/kill/events commands and tests in `apps/coire-api/src/coire_api/cli.py` and `apps/coire-api/tests/unit/test_cli_runs.py`
- [ ] T048 [P] Write operations, diagnosis, kill, cleanup, rollback, and credential-response procedures in `docs/runbooks/container-runs.md`
- [ ] T049 Regenerate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`, then update freshness and contract tests
- [ ] T050 Run Ruff, strict mypy, all non-integration tests, web tests/lint/build, OpenAPI freshness, migration upgrade/downgrade/upgrade, and record evidence in `specs/011-container-run-orchestration/review.md`
- [ ] T051 Build and policy-check affected images, scan for critical CVEs, generate SPDX SBOMs, and record evidence in `specs/011-container-run-orchestration/review.md`
- [ ] T052 Run the full Docker integration suite including confinement and recovery, then record evidence in `specs/011-container-run-orchestration/review.md`
- [ ] T053 Perform convergence analysis and complete every appended task in `specs/011-container-run-orchestration/tasks.md`
- [ ] T054 Manually deploy and verify both real Studios and core-isolation evidence per `quickstart.md`, recording only non-secret run IDs/timestamps in `specs/011-container-run-orchestration/review.md`

## Dependencies and Execution Order

- Phase 1 precedes foundational contracts/persistence.
- Phase 2 blocks all user stories.
- US1 establishes the lifecycle used by US2–US5.
- US2 and US3 may proceed after US1; US4 depends on the public lifecycle and precedes terminal workflow behavior.
- US5 depends on idempotent US1/US4 effects.
- Cross-cutting gates follow all automated implementation work; T054 is operator-only.

## Parallel Opportunities

- Contract tests targeting separate packages are parallelizable where marked `[P]`.
- Telemetry, dashboard/alerts, CLI, and runbook work are independent after wire contracts stabilize.
- Real-cluster verification is deliberately excluded from developer/CI execution.

## Implementation Strategy

The MVP is Phases 1–3: one authenticated, durable, fully cleaned-up Studio run. Then make the
Studio-only boundary structural, add confinement, add revocation/kill, and finally prove recovery.
No phase may weaken network or runtime controls to make a test pass.
