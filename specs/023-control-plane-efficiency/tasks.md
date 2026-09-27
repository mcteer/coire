# Tasks: Quiet, reliable control plane

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts](contracts/runtime-and-deployment.md).
**Delivery**: One PR targeting `feat/012-coire-ops-confirmed-mutations`, fixes #24.
**Pause gate**: Cleared when the user resumed implementation; T002 closed the optional-telemetry governance prerequisite before code.
**Tests**: Required; regression tests precede their corresponding implementation. Mark tasks complete only with evidence.

## Phase 1 — Setup

- [x] T001 Record review findings, scope, research, contracts and planning handoff in `specs/023-control-plane-efficiency/` and create issue #24. Maps FR-011.

## Phase 2 — Foundational prerequisites

- [x] T002 Explicitly amend observability policy in `.specify/memory/constitution.md` with version/sync-impact record, add `docs/adr/0007-lean-control-plane-diagnostics.md`, reconcile `docs/ARCHITECTURE.md`, `docs/ROADMAP.md` and affected `specs/000-bootstrap/`, `specs/008-admin-console/`, `specs/009-observability-stack/`, `specs/012-coire-ops-confirmed-mutations/` expectations; retain mandatory metrics/alerts/audit/local logs and document reduced historical coverage. Maps FR-006/010/011, SC-004.
- [x] T003 Add failing configuration/URL tests in `packages/coire-core/tests/test_settings.py`: reserved password/username characters round-trip, separate console/idle/failure/kill settings, bounds preserve safety and existing active polling. Maps FR-002/004/005/007.
- [x] T004 Implement safe URL construction and validated efficiency/capability settings in `packages/coire-core/src/coire_core/settings.py`; document every new variable in `deploy/compose/README.md`, retaining existing contract models unless a tested addition is necessary. Maps FR-002/003/004/005/006/007/009/010.

## Phase 3 — US1: Trustworthy, quiet deployment (P1)

**Independent verification**: Existing-data credentials are validated from the application network; failed preparation cannot change active files; an installed release survives source changes; health distinguishes liveness/dependencies.

- [x] T005 [P] [US1] Add failing deployment harness tests in `tests/unit/test_control_plane_deploy.py` for complete-secret staging, project isolation/shared lock, required-file types, immutable releases/rollback and cleanup ownership; inject sentinel credentials and assert no output/argv leakage. Maps FR-001/002, SC-005.
- [x] T006 [P] [US1] Extend `apps/coire-api/tests/contract/test_health_api.py` and `test_auth_route_sweep.py` for unchanged liveness/auth, sanitized timeout/database failures and disabled optional-service handling. Maps FR-003/010, SC-003.
- [x] T007 [P] [US1] Add `tests/integration/test_deployment_credentials.py` using isolated PostgreSQL: seed data, mismatch credentials, prove app-network preflight failure despite local readiness, explicitly recover and verify rows survive; cover production/test directory separation. Maps FR-001/002/003, SC-005/006.
- [x] T008 [US1] Implement a project-owned release install/preflight helper in `scripts/coire-deploy.py` with stable manifest/config/image paths, complete credential generations, one project lock, strict file/path ownership checks and protected explicit role recovery. Maps FR-001/002, SC-005.
- [x] T009 [US1] Update `deploy/compose/coire-up`, `coire-down` and `scripts/coire-secrets-init.sh` to use staged generations and explicit recovery, clean all owned released credentials, preserve volumes, and reject unsafe implicit credential replacement. Maps FR-001/002.
- [x] T010 [US1] Make normal startup use installed release/pinned images, add explicit `--build` while preserving `--no-build`, validate app-network auth before migrations/dependent replacement, and use non-creating long bind mounts in `deploy/compose/compose.yaml`; update isolated harness `tests/integration/conftest.py`. Maps FR-001/002/003, SC-005.
- [x] T011 [US1] Sanitize dependency errors and honor enabled capabilities in `apps/coire-api/src/coire_api/routes/health.py`; connect startup preflight to the same application credential/network path without changing `/ready`. Maps FR-003/010, SC-003.
- [x] T012 [US1] Reuse `apps/coire-web/healthcheck/` in changed first-party Dockerfiles under `apps/coire-api/docker/` and `apps/coire-agent/`; replace backend version probes with actual HTTP checks and set normal liveness cadence in `deploy/compose/compose.yaml` independently of safety heartbeats. Maps FR-003/005.
- [x] T013 [US1] Run new regressions plus `tests/integration/test_bringup.py`, `test_topology.py` and `test_restart_isolation.py`; record credential persistence/release-isolation evidence in `specs/023-control-plane-efficiency/verification.md`. Maps FR-011, SC-003/005/006.

## Phase 4 — US2: Bounded background work and safe kills (P1)

**Independent verification**: API owns no moved executor, scheduler starts/stops each once, empty queues/failures back off, WAIT cannot block KILL, failed kill stays pending, profiles never export to disabled backends.

- [x] T014 [P] [US2] Add failing lifecycle/backoff tests in `apps/coire-api/tests/unit/test_scheduler_workers.py` covering seven-worker ownership, partial startup, bounded cancellation, durable-row preservation, successful reset and retained API probes. Maps FR-004/005, SC-006.
- [x] T015 [P] [US2] Add failing kill concurrency/state tests in `apps/coire-api/tests/unit/test_run_executor.py`, `test_run_workflow.py` and `tests/integration/test_run_orchestration.py`: blocked WAIT, unavailable other node, failed kill/no completion audit, idempotency, unplaced kill, CREATE/completion races, immediate revocation and five-second bound. Maps FR-005/012, SC-007.
- [x] T016 [P] [US2] Add profile/exporter/resource regression tests in `tests/unit/test_control_plane_profiles.py` and `tests/integration/test_observability_health.py`, preserving hardening, validating real listeners and proving metrics/alerts without dashboards/history. Maps FR-006/010, SC-004.
- [x] T017 [US2] Implement reusable bounded stop-aware polling/error summaries in `apps/coire-api/src/coire_api/polling.py`; apply to ordinary command loops, scheduler discovery and retained coordinator error paths without slowing active workflow waits or safety cadence. Maps FR-004/005, SC-003.
- [x] T018 [US2] Add scheduler worker supervisor in `apps/coire-api/src/coire_scheduler/workers.py`, move seven workers from `coire_api/app.py` to `coire_scheduler/main.py`, propagate run image settings in Compose, and ensure event-loop-safe sessions/cleanup in `coire_api/db.py` and `coire_scheduler/dbos_runtime.py` where regression evidence requires. Maps FR-004/005/010.
- [x] T019 [US2] Implement one 0.25-second kill scan with an independent execution lane in `coire_scheduler/main.py` and `coire_api/run_executor.py`; reserve per-node concurrency equal to `run_concurrency_cap`, deduplicate in-flight IDs, test simultaneous same-node kills and the four-second node-call budget; exclude KILL from ordinary work and preserve replay IDs. Maps FR-005/012, SC-007.
- [x] T020 [US2] Fix confirmed kill completion and transactional state/token guards in `coire_scheduler/runs.py`, `coire_api/runs.py`, `coire_api/run_executor.py` and `coire_api/routes/admin_runs.py`; cancellation/error cannot falsely complete a kill or revive a token. Maps FR-005/010/012, SC-007.
- [x] T021 [US2] Selectively restore baseline monitoring and optional profiles/configurations under `deploy/compose/` from reviewed feature-009 resources; match collector destinations to enabled diagnostics, avoid startup dependencies on optional services and preserve current network/image policy. Maps FR-006/010, SC-004.
- [x] T022 [US2] Add validated per-service CPU/memory/PID limits, rotated container logs, retention/storage caps and bounded exporter queues/retries in `deploy/compose/compose.yaml` and monitoring configs; leave global OrbStack settings unchanged. Maps FR-004/006.
- [x] T023 [US2] Run lifecycle/backoff/kill/profile tests plus existing acquisition/placement/sharding/run integration and DBOS loop-safety/restart recovery checks; record timing and unchanged safety behavior in `specs/023-control-plane-efficiency/verification.md`. Maps FR-005/011/012, SC-004/006/007.

## Phase 5 — US3: Monitoring does useful work only (P2)

**Independent verification**: Multiple admins share one projection per refresh window; time-only changes do not emit snapshots; hidden streams close; runtime metrics are labelled truthfully.

- [x] T024 [P] [US3] Add failing cache/SSE tests in `apps/coire-api/tests/unit/test_console_service.py` and `tests/contract/test_admin_console.py` for authorization before reuse, single-flight concurrency/expiry/failure, one ledger read, timestamp suppression, meaningful freshness changes and reconnect/disconnect cleanup. Maps FR-007/009/010, SC-001.
- [x] T025 [P] [US3] Add failing browser tests in `apps/coire-web/src/hooks/useEventStream.test.tsx` and `src/App.test.tsx` for visibility/online transitions, reader/timer cleanup, retry reset/jitter bound, retained cursor/reconciliation, reduced effects and runtime/unknown-CPU labels. Maps FR-008/009, SC-002.
- [x] T026 [US3] Refactor `coire_api/console/service.py`, `coire_api/routes/instances.py` and `coire_api/placement/service.py` to reuse one ledger projection and a copy-safe single-flight snapshot cache; retain meaningful health/freshness and use null for unsupported CPU utilization. Maps FR-007/009, SC-001.
- [x] T027 [US3] Integrate semantic event equality/cache TTL, keepalives and bounded stream cleanup in `apps/coire-api/src/coire_api/routes/admin_console.py`, preserving admin authorization and Last-Event-ID behavior. Maps FR-007/010, SC-001.
- [x] T028 [US3] Move stream transport to `apps/coire-web/src/api/eventStream.ts` and update `src/hooks/useEventStream.ts` for hidden-page abort, fresh reconciliation, bounded jittered retry and complete cleanup; preserve inference streaming behavior. Maps FR-008, SC-002.
- [x] T029 [US3] Stabilize affected render state in `apps/coire-web/src/App.tsx`, label existing runtime source/unknown CPU and reduce pervasive blur with reduced-motion/transparency support in `src/styles/app.css`. Maps FR-008/009.
- [x] T030 [US3] Run backend regressions, `tests/integration/test_admin_console_integration.py` and web test/lint/build; record projection counts, visibility behavior and light/dark UI evidence in `specs/023-control-plane-efficiency/verification.md`. Maps FR-011, SC-001/002/006.

## Phase 6 — Cross-cutting validation and one PR

- [x] T031 Add bounded-cardinality console/worker/startup/kill metrics and `coire.scheduler.*` spans, exclude routine liveness tracing, and add dashboard/alert coverage in `deploy/observability/grafana/dashboards/control-plane-efficiency.json` and `deploy/observability/alerts/control-plane-efficiency.yaml`; prove rule loading in baseline monitoring. Maps FR-011.
- [x] T032 Update `docs/runbooks/control-plane-efficiency.md`, `deploy/compose/README.md`, `.env.example`, architecture and related runbooks for release installation, credential recovery, profiles/coverage, resource trial, rollback and API/worker ownership. Maps FR-001/002/003/004/005/006/011.
- [x] T033 Regenerate/check `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts` for any changed contract; amend associated spec contract tests rather than weakening existing gates. Maps FR-010/011.
- [x] T034 Run full format/lint/mypy, Python unit/contract suites and web tests/lint/build, resolving regressions and recording actual pass/skip/environment status in `specs/023-control-plane-efficiency/verification.md`. Maps FR-011, SC-006.
- [x] T035 Run isolated local integration for release install/rollback, credentials, all supported profile combinations, health/restart, acquisition/placement/sharding and run kill/recovery; preserve test project isolation and record explicit unavailable gates in `verification.md`. Maps FR-001/002/003/004/005/006/010/011/012, SC-003/004/005/006/007.
- [x] T036 Build changed production images, run `scripts/image-policy.sh`, existing scans/SBOM/Compose CI gates and validate startup under resource ceilings; record evidence in `verification.md` without disabling checks or adding debug tools. Maps FR-003/006/010/011.
- [x] T040 Restore the historical `0005_observability_health` migration as the parent of acquisition variants; verify a private dump of the live legacy database, restore it into a disposable PostgreSQL container, upgrade to current head with row counts preserved, and check fresh-database migration. Maps FR-001/002/010/011, SC-005/006. This live-discovered prerequisite precedes T037.
- [x] T037 Prepare recoverable core release/data capture, reconcile canonical credential drift if the network preflight proves it, and recreate only affected local core services; verify rows, authenticated health, zero repeated authentication/missing-backend export errors over 60 seconds and before/after resource samples; remove Coire project containers for disabled capabilities and the orphan after replacement health, preserve named volumes/release rollback inputs/unrelated containers, and document evidence in `verification.md`. No Studio operations or automatic OrbStack stop/global cap change. Maps FR-001/002/003/006/011/013, SC-006/008.
- [x] T038 Run final Spec Kit consistency analysis and diff review against `specs/023-control-plane-efficiency/`; verify all thirteen FRs/eight SCs have evidence, safety/governance gates are closed and no secrets/unrelated artifacts are staged. Maps FR-010/011/013.
- [x] T039 Create the single PR using `.github/PULL_REQUEST_TEMPLATE.md`, target `feat/012-coire-ops-confirmed-mutations`, link spec and Fixes #24, list constitution/dependency/licence checks, validation and rollout/rollback evidence; wait for CI and resolve failures. Maps FR-011.

## Dependencies and execution order

T001 → T002 → T003 → T004 → US1 → US2 → US3 → cross-cutting gates/PR. T040 is required before live T037. T005/T006/T007, T014/T015/T016, and T024/T025 are independent test-writing opportunities within their phases. Run failures to establish regressions before implementing their corresponding fixes.

US1 release changes must precede US2 deployment profile integration; US2 worker implementation can be independently tested without profiles. US3 projection/browser work is independently testable but shares app lifecycle/settings, so integrate sequentially. Serialize edits to Compose, settings, app.py and verification.md. T019 and T020 are one safety boundary and require joint race tests before rollout. T037 follows T034–T036 and a recoverable release; T039 follows final review. Parallel markers describe independent work, not automatic permission to spawn implementation agents.

## Delivery strategy

First independently verify US1 as the operational foundation, then workers/kill safety, then console behavior. Deliver all three in the requested single PR. Maintain separate logical commits and explicit measurements; never infer desktop-frame improvement from idle CPU/memory. No tests are marked passed merely because they were skipped or unavailable.
