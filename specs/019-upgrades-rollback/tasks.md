# Tasks: Upgrades and Rollback

**Input**: `specs/019-upgrades-rollback/` spec, plan, research, data model, contracts and quickstart.

**Branch**: `feat/019-upgrades-rollback-plan`

**Tests**: Required by FR-030, Constitution VII and AGENTS.md. Write story tests first, observe the expected failure, then implement and make them pass. No required skipped checks count as done.

**Organization**: Shared foundation, then four independently validated story increments, then release gates. All tasks start unchecked; no work or results transfer from the obsolete dirty 019 worktree.

## Format and execution rules

`- [ ] T### [P?] [US#?] Description with file path`.

`[P]` marks independent test-authoring tasks within the explicitly stated phase prerequisite barrier. It does not authorize sub-agents; follow current delegation instructions. Unmarked tasks execute sequentially unless their dependencies are proven complete. New module/test paths below are intentional implementation targets. Every task maps to requirements through its IDs and the coverage table.

## Phase 1: Setup

- [X] T001 Verify merged baseline, preserve `/Users/mcteer/Projects/coire-019`, record actual branch/HEAD, migration head and required local/native assets in `specs/019-upgrades-rollback/execution-record.md`; do not copy old completion marks (FR-029, FR-030).
- [X] T002 Resolve the executor-network authorization recorded in `specs/019-upgrades-rollback/handoff.md` before applying membership changes; draft `docs/adr/0015-upgrade-recovery-executor.md` with exact fixed-operation authority, bootstrap handover and rejected native daemon; verify the ADR number is still free (FR-016, FR-027, FR-029).

## Phase 2: Foundational — blocks story implementation

- [X] T003 Add all strict release/compatibility/backup, operation, maintenance, inventory, bootstrap, node/executor command, receipt/event and console capability models in `packages/coire-core/src/coire_core/models/upgrades.py`, with exports/job-kind/auth scope changes in the adjacent existing models (FR-002, FR-017, FR-018, FR-019, FR-024, FR-025).
- [X] T004 Test bounds, unknown fields, identity/hash validation, compatibility, replay shapes and every new wire model in `tests/contract/test_upgrade_models.py` (FR-002, FR-017, FR-025, SC-012).
- [X] T005 Add authoritative rows, FK/uniqueness constraints, single-active-operation enforcement and one guarded reversible migration in `apps/coire-api/src/coire_api/db.py` and `apps/coire-api/alembic/versions/0034_upgrades.py`; rebase the number if the migration head changed (FR-009, FR-011, FR-019, FR-020, FR-025).
- [X] T006 Test concurrent claims, monotonic fences, idempotent receipts and empty/populated migration downgrade behavior against real disposable PostgreSQL in `tests/integration/test_upgrade_persistence.py` (FR-011, FR-017, FR-019, FR-025, SC-004, SC-006).
- [X] T007 Add bounded settings, feature enablement, CoireError mapping, live human-admin upgrade authorization and the restricted executor principal in `packages/coire-core/src/coire_core/settings.py`, `errors.py`, and `apps/coire-api/src/coire_api/auth.py`; document every setting and Keychain secret in `deploy/compose/README.md` (FR-024, FR-025, FR-027).
- [X] T008 Extend prepared release manifests and full content hashes in `packages/coire-core/src/coire_core/deploy.py`, `scripts/coire-deploy.py`, `apps/coire-node/install.sh` and `install_runtime.py`; enforce trusted artifact provenance, compatible platform, immutable destinations and controlled interpreter links (FR-001, FR-002, FR-008, FR-021).
- [X] T009 Implement typed shared host/executor ownership and source-independent selection receipts in `packages/coire-core/src/coire_core/deploy.py`, `scripts/coire-deploy.py`, `deploy/compose/coire-up` and `coire-down`; replace cross-namespace PID assumptions and protect referenced releases/secrets from cleanup (FR-025, FR-027).
- [X] T010 Implement durable Core maintenance and ordered admission locking in `apps/coire-api/src/coire_scheduler/maintenance.py`, `coire_api/placement/service.py`, gateway admission and scheduler training/evaluation/image/run/acquisition admission paths; derive affected scope from the fixed cohort/dependency map, preserving unrelated chat; keep callbacks/renewals and cleanup alive during drain (FR-009, FR-010, FR-011, FR-023).
- [X] T011 Implement node maintenance generations, strict inventory registration and startup reconciliation in `apps/coire-node/src/coire_node/maintenance.py`, `agent.py` and `reservations.py`; persist fence acknowledgment before drain and keep unknown owners/holds blocking (FR-009, FR-010, FR-025, FR-029).
- [X] T012 Wire engine/request/shard/image owners to the node fence in `apps/coire-node/src/coire_node/engines.py`, `routes/engines.py`, `routes/sharding.py`, `routes/image_workers.py` and `routes/image_jobs.py`; unload only positively idle residents through existing lifecycle owners (FR-009, FR-010).
- [X] T013 Wire every training/measurement/analysis/extraction/import/component-transfer admission and restart path to strict maintenance in `apps/coire-node/src/coire_node/routes/training*.py` and `training/`; preserve stop-before-release and journal compatibility behavior (FR-009, FR-010, FR-028).
- [X] T014 Wire harness/evaluation/workspace, acquisition/conversion/replication/export and probe owners through maintenance in `apps/coire-node/src/coire_node/routes/{runs,evaluations,workspaces,models,jobs,export,benchmarks,link_probes}.py` and their startup owners (FR-009, FR-010, FR-022).
- [X] T015 Add strict Core inventory for feedback export/comparison/purge, chat/file private erasure and failover/renewal obligations in `apps/coire-api/src/coire_api/upgrades/inventory.py`, associated scheduler feedback/file workflows, and `coire_api/failover/`; do not treat standing reservations as releasable work (FR-010, FR-023, FR-028).
- [X] T016 Test every owner racing fence establishment and startup, collection errors, terminal-but-cleaning work, unknown processes, lease renewals and drain expiry in `tests/unit/test_upgrade_maintenance.py`; assert no forced user-work stops or early hold release (FR-009, FR-010, FR-011, SC-006, SC-009, SC-010).
- [X] T017 Add idempotent acceptance, fixed plans, live actor revalidation, receipt reconciliation and node clients in `apps/coire-api/src/coire_api/upgrades/service.py`, `authorization.py` and `nodes_client.py`; no mutation from API background tasks (FR-019, FR-024, FR-025, FR-026).
- [X] T018 Add audit/event helpers and bounded redacted evidence with stage spans/logs in `apps/coire-api/src/coire_api/upgrades/telemetry.py` and shared node/executor receipt contracts; preserve unacknowledged local receipts until durable ingestion (FR-019, FR-020, SC-008).

**Checkpoint:** Typed authority, complete maintenance coverage and durable recovery metadata exist before accepting any managed upgrade. Bootstrap readiness still prevents premature use.

## Phase 3: US1 — Bad Studio upgrade restores service (P1)

**Goal:** Preserve the source before activation; independently restore it afterward.

**Independent test:** Inject each durable-boundary failure and prove exact-source fresh serving or explicit fenced recovery-required state.

- [x] T019 [P] [US1] Write node/admin-start contracts for authentication, malformed source, replay, missing bootstrap and dropped responses in `tests/contract/test_upgrade_node_routes.py` (FR-002, FR-005, FR-006, FR-024, FR-025, SC-009).
- [X] T020 [P] [US1] Write node guard/executor fault tests for pointer boundaries, corrupt/full journals, PID reuse, wrong healthy digest, missing source and recovery-owner/host restart in `tests/unit/test_upgrade_guard.py` (FR-004, FR-006, FR-021, FR-025, FR-029, SC-001).
- [X] T021 [US1] Implement disk reservation, candidate integrity/import smoke, immutable staging and owned cleanup in `apps/coire-node/src/coire_node/upgrade_executor.py` and `install_runtime.py`; never evict models, datasets or retained sources (FR-001, FR-005, FR-008, SC-009).
- [X] T022 [US1] Implement registry-verified tiny-model pre/post smoke in `apps/coire-node/src/coire_node/upgrade_smoke.py`, using normal bare-engine PID/port/memory accounting, ten tokens, bounded evidence and positive cleanup (FR-003, FR-005, FR-010, SC-001).
- [X] T023 [US1] Implement independent fsynced recovery in `apps/coire-node/src/coire_node/upgrade_guard.py` and `deploy/launchd/com.coire.node-upgrade.plist.template`; extend installer/bootstrap and agent startup so recovery precedes automatic workload resumption (FR-006, FR-025, FR-029, SC-001).
- [X] T024 [US1] Add atomic activation, identity-bound restart, deadline checks, source restoration and recovery-required fencing in `apps/coire-node/src/coire_node/upgrade_executor.py`, preserving normal non-upgrade engine adoption (FR-004, FR-006, FR-021, FR-026, SC-001).
- [X] T025 [US1] Implement the authenticated maintenance/status/execute/cancel node routes in `apps/coire-node/src/coire_node/routes/upgrades.py` and register them in `agent.py`; reject stale generation/source and arbitrary path/command input (FR-009, FR-019, FR-024, FR-025).
- [X] T026 [US1] Implement node upgrade DBOS stages, durable receipt reconciliation and bounded failure/restoration observation in `apps/coire-api/src/coire_scheduler/upgrades.py`; resume accepted operations after scheduler restart (FR-005, FR-006, FR-019, FR-025).
- [X] T027 [US1] Expose minimal audited release registration/node-start/status routes in `apps/coire-api/src/coire_api/routes/admin_upgrades.py` and `app.py` using the shared service; enable only verified bootstrap targets (FR-002, FR-019, FR-020, FR-024, FR-029).
- [x] T028 [US1] Prove broken candidate, post-switch timeout, helper restart and exact source serving in isolated native `tests/integration/test_upgrade_node.py` using the ≤1 GB tiny fixture; record measured restoration deadlines (FR-003, FR-006, FR-030, SC-001, SC-012).

## Phase 4: US2 — Good Studio change and compatible rollback (P1)

**Goal:** Upgrade one Studio safely, refresh exact qualifications and support later rollback.

**Independent test:** Complete upgrade/rollback with fresh serving and paired evidence while concurrent work cannot cross maintenance.

- [X] T029 [P] [US2] Write manual rollback/cancel and concurrent operation tests in `tests/contract/test_upgrade_rollback.py`, covering incompatible retained formats, revocation before activation and cancellation after activation (FR-007, FR-011, FR-017, FR-024, FR-026, SC-006).
- [X] T030 [P] [US2] Write same-version content change and exact-pair qualification tests in `tests/unit/test_upgrade_qualification.py`; preserve immutable historical profiles and published artifacts (FR-012, FR-021, FR-028, SC-002, SC-007).
- [x] T031 [US2] Bind full environment identity into node status and current qualification in `packages/coire-core/src/coire_core/models/node.py`, `apps/coire-node/src/coire_node/agent.py`, `rendering_identity.py`, `training/datasets.py` and Core capability lookups; retain Apply/training/evaluation/image gates (FR-004, FR-012, FR-021, FR-022, SC-002).
- [X] T032 [US2] Fence/drain the required peer before activation and add exact-pair real collective and stale observation invalidation in `apps/coire-node/src/coire_node/link_probes.py` and Core link-probe coordination; failed candidate qualification restores only the changed Studio, while failed source-pair recovery stays paired-ineligible (FR-003, FR-011, FR-012, FR-022, SC-007).
- [X] T033 [US2] Implement retained-source compatibility, manual rollback, cancellation and desired/observed drift projection in `apps/coire-api/src/coire_api/upgrades/service.py`, `routes/admin_upgrades.py` and scheduler/node upgrade state machines (FR-007, FR-017, FR-018, FR-021, FR-026, SC-002).
- [X] T034 [US2] Extend `tests/integration/test_upgrade_node.py` with successful change/rollback, paired smoke, busy work/drain abort, authorization revocation and preserved journal/asset state; test both pre/post activation cancellation paths (FR-003, FR-007, FR-010, FR-026, FR-028, SC-002, SC-007, SC-010, SC-011).
- [X] T035 [US2] Exercise simultaneous Studio/Core requests, stale receipts and repeated activation delivery with real database fencing in `tests/integration/test_upgrade_persistence.py`; require one cluster owner and no duplicate external effect (FR-011, FR-025, SC-006).

## Phase 5: US3 — Core rollout survives API/scheduler replacement (P1)

**Goal:** Execute migration and sequential service change through a separate hardened recovery owner; restore the whole changed cohort on failure.

**Independent test:** Fail each stage and reboot the executor/isolated host; verify exact-source service cohort, mounts, history and recovery ownership.

- [X] T036 [P] [US3] Write internal claim/grant/receipt/smoke contracts and hostile Docker request cases in `tests/contract/test_upgrade_executor.py`; cover project escape, exec/pull/prune, credential scope, replay and oversized/redaction payloads (FR-016, FR-024, FR-025, FR-027).
- [X] T037 [P] [US3] Write service ordering, failed migration, whole-cohort rollback and interrupted bootstrap tests in `tests/unit/test_upgrade_cohort_orchestration.py` (FR-013, FR-014, FR-015, FR-016, FR-017, FR-029, SC-003, SC-004).
- [X] T038 [US3] Add the separate workspace service and hardened image in `apps/coire-upgrade-executor/pyproject.toml`, `Dockerfile`, `src/coire_upgrade_executor/main.py`, root `pyproject.toml` and lockfile; use existing pinned libraries, one process and no shell/Docker CLI (FR-016, FR-027, FR-030).
- [X] T039 [US3] Implement exact Docker HTTP method/path/target allowlisting and strict frozen-Compose translation in `apps/coire-upgrade-executor/src/coire_upgrade_executor/docker.py`; preserve aliases, health/dependency gates, ports and existing mounts; reject unsupported config and implicit new volumes (FR-002, FR-014, FR-021, FR-027).
- [X] T040 [US3] Implement durable effect intent, reality reconciliation, exclusive ownership, bounded deadlines and idempotent receipt upload in `apps/coire-upgrade-executor/src/coire_upgrade_executor/journal.py` and `executor.py`; retain recovery authority during API outage (FR-006, FR-016, FR-019, FR-025).
- [x] T041 [US3] Implement initial not-ready bootstrap and incumbent-supervised executor handover in `apps/coire-upgrade-executor/src/coire_upgrade_executor/bootstrap.py`, `scripts/coire-deploy.py` and `deploy/compose/coire-up`; preserve both restartable owners until sole-writer ownership and candidate stability are proven (FR-006, FR-016, FR-029).
- [X] T042 [US3] Implement compatibility preflight in `apps/coire-api/src/coire_api/upgrades/compatibility.py` and fixed artifact/backup preparation modes in `scripts/coire-deploy.py`; require a ≤24-hour source-schema backup with isolated restore-validation receipt, without credential rotation or production restore (FR-002, FR-013, FR-017, FR-027, FR-028).
- [X] T043 [US3] Implement one-shot migration execution and expected-schema reconciliation in `apps/coire-upgrade-executor/src/coire_upgrade_executor/executor.py` and a typed migration receipt entrypoint under `apps/coire-api/src/coire_api/upgrades/`, wired through `apps/coire-api/docker/migrate.Dockerfile`; require terminal zero before replacement and retain maintenance on uncertain schema outcome (FR-013, FR-017, SC-004).
- [X] T044 [US3] Implement fixed-cohort sequential replacement and post-service digest/health checks in `apps/coire-upgrade-executor/src/coire_upgrade_executor/executor.py`; retain source containers, configuration and volume references until commit (FR-014, FR-021, FR-027, SC-003).
- [X] T045 [US3] Implement reverse-order restoration of every changed/stopped source and final source verification in `apps/coire-upgrade-executor/src/coire_upgrade_executor/executor.py`; never downgrade DB or release uncertain maintenance (FR-006, FR-015, FR-016, FR-017, SC-003).
- [X] T046 [US3] Implement executor-only claim, activation-grant, receipt and operation-scoped gateway smoke routes in `apps/coire-api/src/coire_api/routes/internal_upgrades.py`, `upgrades/service.py` and `app.py`; revalidate human authority separately before migration and runtime activation and account smoke through normal Studio ownership (FR-003, FR-016, FR-024, FR-025).
- [x] T047 [US3] Implement Core operation acceptance/orchestration and startup maintenance reconciliation in `apps/coire-api/src/coire_scheduler/upgrades.py`, `main.py` and admin upgrade routes; drain both Studios' renewal obligations before API/scheduler replacement; unrelated-only cohorts drain their dependency scope without interrupting chat (FR-009, FR-010, FR-013, FR-016, FR-023).
- [X] T048 [US3] Integrate executor selection receipts with host `current` reconciliation, protected cleanup and immutable credential references in `scripts/coire-deploy.py`, `packages/coire-core/src/coire_core/deploy.py` and `tests/unit/test_control_plane_deploy.py`; cover source deletion and concurrent up/down/cleanup (FR-019, FR-025, FR-027, SC-011).
- [X] T049 [US3] After the recorded network approval, add only the designed executor networks, mounts, secret, limits and healthcheck in `deploy/compose/compose.yaml` and update exact role expectations in `tests/integration/test_topology.py`; retain all proxy flags and denial tests (FR-016, FR-027, FR-029).
- [ ] T050 [US3] Prove migration/no-replacement, each service failure, API/scheduler loss, gateway outage budget, 100 unrelated streams and valid failover hand-back in isolated `tests/integration/test_upgrade_control_plane.py` (FR-013, FR-014, FR-015, FR-016, FR-023, SC-003, SC-004, SC-005).
- [ ] T051 [US3] Prove first-install interruption, executor image handover, host/executor restart at every journal boundary and conflicting ownership in `tests/integration/test_upgrade_bootstrap.py`; no candidate may be its sole rollback owner (FR-006, FR-025, FR-029, SC-003, SC-009, SC-012).
- [X] T052 [US3] Add preservation tests in `tests/integration/test_upgrade_preservation.py` for credential bytes/mount generation, dataset/adapter hashes, opt-out/purge state, evaluation/native training history and project isolation; reject pre-018 incompatible rollback without silently quarantining users (FR-017, FR-027, FR-028, SC-011).

## Phase 6: US4 — Console and CLI operations (P2)

**Goal:** Administrators observe truthful release/readiness state and operate upgrades through durable reconnecting interfaces.

**Independent test:** Console and CLI complete the same workflow across API outage, replay and authorization loss without duplicate mutation.

- [X] T053 [P] [US4] Write all remaining admin route and CLI contract cases in `tests/contract/test_upgrade_admin.py` and `tests/unit/test_upgrade_cli.py`, including release registration trust, replay, events, scope, redaction and source/target mismatch (FR-002, FR-018, FR-019, FR-024, FR-025, SC-008).
- [X] T054 [P] [US4] Write generated-type client/page tests for readiness, maintenance acknowledgment, double-click deduplication, progress reconnect, recovery-required and keyboard states in `apps/coire-web/src/pages/UpgradesPage.test.tsx` (FR-018, FR-019, FR-023, SC-008).
- [X] T055 [US4] Complete admin release/target listing, rollback/cancel and durable paginated/SSE event routes in `apps/coire-api/src/coire_api/routes/admin_upgrades.py`, with live access rechecks, bounded evidence and snapshot reset (FR-007, FR-018, FR-019, FR-020, FR-021, FR-026, SC-008).
- [X] T056 [US4] Add the narrowly typed upgrade client in `packages/coire-core/src/coire_core/client.py` and API-only `coire upgrades` commands in `apps/coire-api/src/coire_api/cli.py`; preserve idempotency and reconnect by job ID without Docker/SSH access (FR-018, FR-019, FR-024, SC-008).
- [X] T057 [US4] Expose admin upgrade capability in `packages/coire-core/src/coire_core/models/console.py` and `apps/coire-api/src/coire_api/routes/admin_console.py`, regenerate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`, and add typed calls in `src/api/client.ts` (FR-018, FR-024, FR-030).
- [X] T058 [US4] Implement `apps/coire-web/src/pages/UpgradesPage.tsx` and navigation in `App.tsx`, using `src/hooks/useEventStream.ts`, safe state labels, keyboard/focus support and readable blockers; no direct fetch outside `src/api/` (FR-018, FR-019, FR-021, FR-023).
- [X] T059 [US4] Verify full console/CLI reconnect, mutation retry, revoked access and cancellation-to-restoration flows in `apps/coire-web/src/pages/UpgradesPage.test.tsx` and `tests/contract/test_upgrade_admin.py`; measure ≤10-second replay after readiness (FR-018, FR-019, FR-024, FR-026, SC-008).

## Phase 7: Observability, documentation and release gates

- [X] T060 Complete operation/phase/owner spans, bounded logs and low-cardinality failure, duration, drift, maintenance and recovery-age metrics in API/node upgrade telemetry and `apps/coire-upgrade-executor/src/coire_upgrade_executor/telemetry.py`; test diagnostics-off audit/receipt completeness (FR-019, FR-020, FR-021, SC-008).
- [X] T061 Add `deploy/observability/alerts/upgrades.yaml`, `grafana/dashboards/upgrades.json`, `tests/upgrades.test.yaml` and actual scrape/rule/dashboard registration in `deploy/compose/`; test deadline/stalled/uncertain-restoration alerts including absent executor heartbeat, lean and diagnostics modes (FR-020, SC-001, SC-012).
- [X] T062 Write `docs/runbooks/upgrades.md`, finalize `docs/adr/0015-upgrade-recovery-executor.md`, and update `docs/ARCHITECTURE.md`, `docs/ROADMAP.md` and `deploy/compose/README.md` for bootstrap, fixed cohort, backup preparation, maintenance, cancellation, rollback limits, qualification, kill/recovery and source retention (FR-017, FR-022, FR-027, FR-029, FR-030).
- [ ] T063 Add executor build/scan/SBOM and isolated recovery/native integration jobs to the existing `.github/workflows/` CI configuration, preserving all hardening/type/test/OpenAPI gates and excluding real Studios (FR-030, SC-012).
- [ ] T064 Run full required Python/web/type/OpenAPI checks and executor image build/scan/SBOM; record exact commands/results in `specs/019-upgrades-rollback/execution-record.md`, resolving all failures without weakening checks (FR-030, SC-012).
- [ ] T065 Run the complete disposable PostgreSQL/Compose, deployment race, bootstrap crash and retention suites from `specs/019-upgrades-rollback/quickstart.md`; record measured Core results and no-skip evidence in `execution-record.md` (FR-030, SC-003, SC-004, SC-005, SC-006, SC-009, SC-011, SC-012).
- [X] T066 Run isolated native tiny-model upgrade/failure/rollback and owner-drain integration from `specs/019-upgrades-rollback/quickstart.md`, with exact artifact identities and restoration timing in `execution-record.md` (FR-030, SC-001, SC-002, SC-007, SC-009, SC-010, SC-012).
- [ ] T067 Perform authorized real-Studio and Core maintenance acceptance from `specs/019-upgrades-rollback/quickstart.md`, including real paired collective, representative training/image/agent drain, failed candidate, manual rollback, preserved data and final hold cleanup; record actual measurements in `execution-record.md` (FR-028, FR-030, SC-001, SC-002, SC-003, SC-005, SC-007, SC-010, SC-011, SC-012).
- [ ] T068 Reconcile all requirements and acceptance evidence, mark only completed tasks in `specs/019-upgrades-rollback/tasks.md`, and open a PR using `.github/pull_request_template.md` with spec, constitution compliance, new service/dependency explanation, required checks and real-cluster evidence (FR-020, FR-027, FR-030, SC-012).

## Dependencies and Execution Order

Setup → foundation → US1 → US2 → US3 → US4 → release gates is the default sequential path. US3's executor internals can start after foundation independently of US1/US2, but its end-to-end smoke and Core drain validation depend on the node fence/routes and qualification work in US1/US2. US4 may author contract/UI tests after shared contracts; integration waits for all action routes. No broad parallel label overrides shared-file dependencies.

Within each story, its test-authoring pair can run independently after the previous phase's contracts/foundations exist: US1 T019/T020; US2 T029/T030; US3 T036/T037; US4 T053/T054. These pairs edit different files and require no unfinished peer task. Implementation tasks follow their tests. Shared `service.py`, routes, scheduler and deployment files must be edited serially. T049 cannot run before the explicit network permission gate is satisfied, even if executor code is ready.

T063 precedes release-gate execution. T064–T067 require the completed implementation and documentation, with concrete prerequisites provisioned first. Fixes discovered in validation require the affected checks to be rerun. T068 requires every mandatory gate, not merely passing unit tests.

## Requirement Coverage

| Requirement | Implementation and validation tasks |
|---|---|
| FR-001 | T008, T021 |
| FR-002 | T003, T004, T008, T019, T027, T039, T042, T053 |
| FR-003 | T022, T028, T032, T034, T046 |
| FR-004 | T020, T024, T031 |
| FR-005 | T019, T021, T022, T026 |
| FR-006 | T019, T020, T023, T024, T026, T028, T040, T041, T045, T051 |
| FR-007 | T029, T033, T034, T055 |
| FR-008 | T008, T021 |
| FR-009 | T005, T010–T014, T016, T025, T047 |
| FR-010 | T010–T016, T022, T034, T047 |
| FR-011 | T005, T006, T010, T016, T029, T032, T035 |
| FR-012 | T030–T032 |
| FR-013 | T037, T042, T043, T047, T050 |
| FR-014 | T037, T039, T044, T050 |
| FR-015 | T037, T045, T050 |
| FR-016 | T002, T036–T038, T040, T041, T045–T047, T049, T050 |
| FR-017 | T003, T004, T006, T029, T033, T037, T042, T043, T045, T052, T062 |
| FR-018 | T003, T033, T053–T059 |
| FR-019 | T003, T005, T006, T017, T018, T025–T027, T040, T048, T053–T056, T058–T060 |
| FR-020 | T005, T018, T027, T055, T060, T061, T068 |
| FR-021 | T008, T020, T024, T030, T031, T033, T039, T044, T055, T058, T060 |
| FR-022 | T014, T031, T032, T062 |
| FR-023 | T010, T015, T047, T050, T054, T058 |
| FR-024 | T003, T007, T017, T019, T025, T027, T029, T036, T046, T053, T056, T057, T059 |
| FR-025 | T003–T007, T009, T011, T017, T020, T023, T025, T026, T035, T036, T040, T046, T048, T051, T053 |
| FR-026 | T017, T024, T029, T033, T034, T055, T059 |
| FR-027 | T002, T007, T009, T036, T038, T039, T042, T044, T048, T049, T052, T062, T068 |
| FR-028 | T013, T015, T030, T034, T042, T052, T067 |
| FR-029 | T001, T002, T011, T020, T023, T027, T037, T041, T049, T051, T062 |
| FR-030 | T001, T028, T038, T057, T062–T068 |
| SC-001 | T020, T022–T024, T028, T061, T066, T067 |
| SC-002 | T030, T031, T033, T034, T066, T067 |
| SC-003 | T037, T044, T045, T050, T051, T065, T067 |
| SC-004 | T006, T037, T043, T050, T065 |
| SC-005 | T050, T065, T067 |
| SC-006 | T006, T016, T029, T035, T065 |
| SC-007 | T030, T032, T034, T066, T067 |
| SC-008 | T018, T053–T056, T059, T060 |
| SC-009 | T016, T019, T021, T051, T065, T066 |
| SC-010 | T016, T034, T066, T067 |
| SC-011 | T034, T048, T052, T065, T067 |
| SC-012 | T004, T028, T051, T061, T063–T068 |

## Implementation Strategy

US1 is the first demonstration: deliberate bad candidate restores the exact source. Add US2 success/rollback and qualification, US3 self-recovering Core rollout, then US4 complete operator flows. Keep intermediate commits green, with tests and observability beside their code. Do not deploy partial recovery support or mark the feature complete before all 68 tasks and required acceptance evidence are satisfied.

- [X] T069 Close acceptance-discovered Core recovery gaps: select a verified Studio copy permitted by the existing image/chat coexistence gate before pinning the serving smoke; preserve authenticated canonical image cancellation, admin run kill and key revocation during maintenance; recognize a successful node KILL cleanup receipt alongside REMOVE while pending/failed commands remain blocking; prove regressions and refreshed actual composed recovery with image residency in `smoke_instance.py`, `admission.py` and tests (FR-006, FR-009, FR-010, FR-012, SC-003, SC-005). Complete before T068.

- [X] T070 Resolve the measured native lease-clock refusal without allowing future snapshots or extending any deadline: wait for an already received authenticated snapshot's sampled time inside the original two-second fetch budget, recheck it after early event-loop timer wakeups, retain its exact expiry, and prove bounded refusal for persistent future observations in `training/lease_snapshot.py` and tests. Stage immutable corrected native wheels and repeat active-image/agent acceptance and affected release gates before T068 (FR-009, FR-010, SC-009, SC-012).

- [X] T071 Apply the owner's 24 GiB Mini resource-cleanup requirement: stop unused local CI-prototype VMs, remove positively unused 016 test containers/images without production/data/selected-release damage, bound disposable PostgreSQL memory/PIDs, identify its owning process, test exceptional-body/startup cleanup and verify zero remaining test databases; document post-run and host-restart cleanup in `tests/training_postgres.py`, `tests/unit/test_disposable_postgres_cleanup.py`, the runbook and execution record (FR-027, FR-030, SC-011, SC-012). Hosted-native CI remains isolated; do not leave a local VM running between checks.

- [X] T073 Implement the owner-approved distinct expired-operation recovery intent: shared strict contracts, authenticated/audited idempotent admin acceptance, original job/generation/plan/scope and exact Source/peer binding, once-captured separate bounded budget, exclusive durable recovery-owner transfer and verified immutable legacy-support deployment without sealed Source patching or manual authority edits. Preserve original deadlines/receipts/failure history; link actual fresh Source-serving, physical cleanup, exact-pair qualification and data preservation to explicit after-deadline resolution, never original upgrade success or copied qualification. Cover authorization, conflict/replay, cancellation/expiry, lost responses and every handover/crash boundary; add observability/runbook/OpenAPI/web types and prove the supported protocol before resolving the retained native operation. Required before T072 live reconciliation and affected T064–T068 gates (FR-031–FR-034, Constitution III/IV/VI/VII).

  2026-10-13: implemented the strict human-admin cancellation route for exact
  separate intent authority, with atomic audit/immutable cancellation receipt,
  concurrency/lost-response replay and post-commit observability. Actual
  PostgreSQL verifies pre-transfer/post-transfer/accepted-successor revocation,
  post-deadline cancellation, demoted initiator, retained history/fences/slots and
  SQL immutability. OpenAPI/web types, cancellation alert/panel and operator
  runbook updated. This completes the cancellation subpath; immutable support
  installation, standalone/multihop/changed-boot handover, qualification and
  linked resolution remain required. T073 remains unchecked.

- [X] T072 Close the acceptance-discovered interrupted collective lifecycle gap: own and clean timed-out launcher/SSH descendants, persist actual failed/interrupted dispatch and cleanup evidence, and provide typed generation-scoped reconciliation for an unresolved retained collective without relaunch, fabricated observations, deadline renewal or manual authority edits. Cover timeout/lost-response/restart boundaries in coire-core contracts and native probe/collective tests, stage immutable corrected artifacts, resolve the existing recovery-required operation through the authenticated protocol and repeat paired/workload acceptance before T068 (FR-009, FR-010, FR-025, FR-028, SC-009, SC-011, SC-012).

- [X] T074 Close the acceptance-discovered unpublished preparation gap: verify a
  released predecessor before creating any new native preparation; prove refusal
  leaves no namespace. Add authenticated, audited cancellation authority for an
  exact accepted, never-activated operation and an immutable support producer
  that archives only positively unclaimed, effect-free preparation bytes before
  invoking ordinary unstarted cancellation. Preserve all original recovery
  history, Source processes, deadlines and model work; cover authorization,
  races, unknown effects, lost responses and archive replay. Reconcile actual
  generation 61 before repeating workload acceptance (III/IV/VI/VII, FR-006,
  FR-025, FR-030).

- [X] T075 Provide audited legacy-native acknowledgement of the already committed
  after-deadline resolution, without a new probe, qualification, execution
  budget, original deadline change or rewritten receipt. Bind the exact canonical
  resolution and actual Source supervision; preserve and hash the existing native
  receipt prefix; prove loss/replay/tampering and old-reader compatibility. Verify
  ordinary next-upgrade acceptance and Source rollback after acknowledgement,
  then repeat required workload gates (FR-031–FR-034, III/IV/VI/VII).

- [ ] T076 Close the helper-restart-discovered pending Source collective gap: distinguish temporary unavailable live-owner observations from measured failed qualification in `upgrade_workflow.verify_source_step`, retain the original recovery deadline, smoke scope and receipts while retrying the exact collective, and cover success/failure/expiry/restart. Preserve failed generation 78 and resolve it through the authenticated distinct recovery protocol under the owner's standing authorization; install immutable corrected artifacts and repeat helper-restart, cancellation/revocation and affected gates before T068 (FR-009, FR-025, FR-030–FR-034; III/IV/VI/VII).

- [ ] T077 Close recovery-after-adoption fixture provenance: preserve the canonical adoption and original fixture definition while capturing typed current Source/guard children through positive runtime/PID/birth/UID/module and inherited-lock checks. Retire only an idle fenced acceptance fixture through recorded once-only signals, positively verify all recorded retirements and both kernel locks, and let cold recovery consume this immutable provenance without rewriting native claims, selectors, receipts or deadlines. Cover foreign roots, changed children/definition, incomplete retirement, live actors and lost signal responses; recover generation 78 and repeat T076 acceptance and required gates (FR-025, FR-030–FR-034; III/IV/VII).

- [ ] T078 Fix retained smoke ownership capacity without discarding evidence or accepting unknown owners: keep 64 active-scope capacity, separately bound completed history to 4,096 namespaces and 4 MiB of owner metadata, validate every retained owner, and prove missing/unknown/active-overflow/oversized refusal. Preserve authenticated legacy inventory and independently attest all real engine ownership sources in the generation 78 fixture retirement record; stage immutable corrected support, recover the fenced operation and repeat helper-restart and affected gates before T068 (FR-009, FR-025, FR-030–FR-034; III/VI/VII).

- [ ] T079 Close cancelled, expired, unstarted recovery safely: retain exact audited cancellation and reject every issued effect/owner payload; permit positive installed unstarted observation after expiry, then atomically close only the separate intent's slot while preserving original fences, history and deadlines. Add an exact-preimage authenticated/audited constraint preparation for the known 0034 source, retain no-effect database enforcement and all recovery rows, cover cancellation scope tampering, expiry, audit loss/replay and foreign schema, regenerate OpenAPI/types, verify image gates and reconcile the actual cancelled generation 78 attempt before T068 (FR-031–FR-034; III/IV/VII).

- [ ] T080 Close the positively retired pre-projection cold-owner and zero-original-dispatch recovery gaps: retain exact cold claim, activation and cancellation in an audited superseded closure only after installed observation proves no downstream effects and positive kernel retirement; reject unknown effect records. Produce replayable positive absence evidence for an accepted empty original-dispatch plan without manufacturing old collective history. Preserve completed legacy Source smoke evidence through a bounded compatibility archive before authentic Source inventory, restore generation 78, repeat acceptance and all affected gates (FR-025, FR-030–FR-034; III/IV/VI/VII).

- [ ] T081 Reconcile partial Source handback safely after a fresh-launch recovery: distinguish the positively recorded Source guard from the Source node in launch inventory without excluding unknown processes; capture exact independently ready Source/helper and old handback intent after retirement/expiry in a typed authenticated metadata observation, then permit a new explicitly audited continuation without rewriting the old deadline or fabricating a within-budget receipt. Preserve all Source smoke, three successful paired results, legacy smoke archive and original Core fences; complete generation 78 and affected acceptance/gates (FR-025, FR-030–FR-034; III/IV/VII).
