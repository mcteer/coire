# Tasks: Evaluation Verbs

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts](contracts/), [quickstart.md](quickstart.md).
**Branch**: `feat/017-evaluation-verbs`. **Status**: 70/70 tasks complete; review prepared. Admission remains disabled; node collection-budget limitation remains recorded.
**Tests**: Explicitly required by FR-029 and Constitution VII. Write behavioral tests before their implementation, observe their expected failure, then finish applicable checks before committing. No gate weakening or required skips.

Task IDs are sequential. `[P]` means independent files within the stated phase/test batch after all preceding shared prerequisites. Story labels preserve spec IDs. Paths are repository-relative; new modules are intentional design targets. Regenerate OpenAPI/TS alongside each contract/route change, not only at the final gate. Every mutation, error, cancellation and new phase gets observability/audit alongside code; later dashboard tasks complete presentation.

## Phase 1: Setup

Capture the shipped baseline and implementation evidence plan; do not change runtime behavior.

- [X] T001 Freeze sanitized 016 recipe, resolved payload and checkpoint identity fixtures under `tests/fixtures/evaluations/legacy_training/`, with source digest provenance in `specs/017-evaluation-verbs/execution-record.md`; retain real private evidence outside Git (FR-024, SC-011).
- [X] T002 Create the acceptance matrix and result-record structure in `specs/017-evaluation-verbs/execution-record.md` from `quickstart.md`, including isolated CI prerequisites and real Studio model/judge/training prerequisites (FR-029, FR-030).

## Phase 2: Foundation

Shared contracts, durable execution and safety must be complete before any story. Red tests may be written together; production commits must finish with their applicable checks green.

- [X] T003 [P] Write strict suite/run/result/judge/workspace/event contract tests in `packages/coire-core/tests/test_evaluation_contracts.py`, including finite scores, complete identities, bounds and subject counts (FR-001, FR-005, FR-009, FR-015, FR-025).
- [X] T004 [P] Write frozen v1 serialization and discriminated v2 parsing tests in `packages/coire-core/tests/test_training_evaluation_contracts.py` using the captured 016 fixtures; cover untouched held-out-loss defaults (FR-003, FR-004, FR-024, SC-011).
- [X] T005 Add evaluation wire models in `packages/coire-core/src/coire_core/models/evaluation.py` and schedule types in `packages/coire-core/src/coire_core/models/training_evaluation.py`; define separate v2/document types beside public v1 classes in `models/training.py`, without circular imports or changed v1 serialization (FR-001, FR-003, FR-005, FR-015, FR-024).
- [X] T006 Update versioned embedding boundaries in `packages/coire-core/src/coire_core/models/training_node.py`, `models/node.py`, `models/runs.py`, and training validation/detail envelopes; add bounded settings/errors in `coire_core/settings.py` and `coire_core/errors.py` without changing v1 serialization (FR-015, FR-020, FR-024, FR-030).
- [X] T007 Add bounded authored harness/task/rubric/pairwise manifests and fixtures to `packages/coire-core/src/coire_core/evaluation_suites/`, package them for API/agent discovery and verify content digests/license attribution in `packages/coire-core/tests/test_evaluation_catalog.py` (FR-001, FR-021, FR-025).
- [X] T008 Add catalog/group/run/attempt/result/evidence/trigger/pin/event/measurement tables, immutability and uniqueness constraints in `apps/coire-api/src/coire_api/db.py` and one reversible `apps/coire-api/alembic/versions/0032_evaluation_verbs.py`; seed the standard harness catalog with system audit attribution and preserve old training/harness rows (FR-001, FR-017, FR-023, FR-024, FR-026).
- [X] T009 Test migration up/down and unique/fenced terminal commits against populated 016 state in `apps/coire-api/tests/integration/test_evaluation_migration.py`, including no JSON rehash and retirement-safe history (FR-017, FR-024, SC-008, SC-011).
- [X] T010 Implement immutable catalog registration and exact-target/owner authorization in `apps/coire-api/src/coire_api/evaluation/catalog.py` and `authorization.py`, including model-ID/base-artifact self-judge refusal before any worker launch and audit attribution (FR-002, FR-008, FR-009, FR-020, SC-003).
- [X] T011 Implement bounded private evidence storage, quota reservations, authenticated lookup, digest checks and expiry in `apps/coire-api/src/coire_api/evaluation/evidence.py`, using the existing private training-data volume namespace (FR-017, FR-027).
- [X] T012 Write authenticated idempotent workspace preparation/collection contract tests in `apps/coire-node/tests/contract/test_evaluation_workspaces.py`, covering hostile paths, wrong fences, unsupported workload versions, oversized bytes and digest mismatch (FR-020, FR-021, FR-024, FR-027).
- [X] T013 Implement bounded evaluation workspace staging in `apps/coire-node/src/coire_node/evaluations.py` and `routes/evaluations.py`, register through `apps/coire-node/src/coire_node/agent.py`, and extend typed `coire_api/nodes_client.py` transport; retain the existing fixed agent argv and hardening (FR-021, FR-024, FR-027).
- [X] T014 Write deterministic harness/task/judge worker tests in `apps/coire-agent/tests/test_evaluation_runner.py`, proving four harness categories, scoring ranges, no generated-code execution, bounded retries and null aggregate on infrastructure failure (FR-010, FR-014, FR-015, FR-021, FR-025, SC-005).
- [X] T015 Add typed dispatch in `apps/coire-agent/src/coire_agent/__main__.py` and phase/scorer/judge modules `evaluation.py`, `evaluation_tasks.py`, `evaluation_judge.py`; reuse `evals.py`, fixed fixtures and gateway exact transport with no user tools (FR-001, FR-008, FR-010, FR-014, FR-015, FR-021, FR-025).
- [X] T016 Add internally bound evaluation run purpose, exact READ grants, private/unverified admission and strict collected-result validation in `apps/coire-api/src/coire_api/runs.py`, `run_executor.py`, `run_tokens.py` and `gateway/targets.py`; preserve normal user publication/write gates (FR-002, FR-005, FR-010, FR-020, FR-021, FR-026).
- [X] T017 Implement durable phase execution and immutable result finalization in `apps/coire-api/src/coire_scheduler/evaluations.py` and `coire_api/evaluation/service.py`, with sequential base/candidate/judge ownership, child-run recovery and no duplicate generation after lost collection acknowledgment (FR-005, FR-008, FR-014, FR-015, FR-017, FR-023).
- [X] T018 Implement exact evaluation coexistence measurements/profiles, ledger/sandbox admission, queue bounds and live serving-priority guard in `apps/coire-api/src/coire_scheduler/evaluation_measurements.py` and `evaluation_guard.py`; reuse node/gateway measured telemetry, never training profiles (FR-012, FR-015, SC-006).
- [X] T019 Test measurement sample completeness/profile invalidation, no-swap/pin/lease protection, telemetry staleness and held resources on lost liveness in `apps/coire-api/tests/integration/test_evaluation_admission.py` (FR-012, FR-015, SC-006).
- [X] T020 Implement shared suite registration/retirement/list, run/group history/detail/SSE and measurement endpoints in `apps/coire-api/src/coire_api/routes/admin_evaluation_runs.py`, `evaluation/events.py` and `app.py`; use generated strict response models and RFC 9457 errors (FR-001, FR-005, FR-017, FR-020, FR-028).
- [X] T021 Cover every shared admin/measurement endpoint, catalog immutability, active-owner rules, idempotency, bounded pages and SSE replay/reset in `apps/coire-api/tests/contract/test_evaluation_catalog_api.py` and `test_evaluation_measurements_api.py` (FR-001, FR-002, FR-017, FR-020, FR-028).
- [X] T022 Instrument API/scheduler/node/agent evaluation paths with content-free spans/logs and bounded metrics in `apps/coire-api/src/coire_api/evaluation/telemetry.py`, `coire_scheduler/evaluations.py`, `apps/coire-node/src/coire_node/evaluations.py` and `apps/coire-agent/src/coire_agent/evaluation.py` (FR-027, FR-028).
- [X] T023 Wire evaluation dispatch, cancel/deadline/owner-revocation recovery and evidence/pin cleanup into `apps/coire-api/src/coire_scheduler/main.py`, `workers.py` and `evaluations.py`; keep reconciliation active when new admission is disabled (FR-015, FR-022, FR-023, FR-027, FR-030).
- [X] T024 Test restart after each phase, lost acknowledgments, duplicate collection, deadlines, revocation, reachable kill and unknown-stop reservations in `apps/coire-api/tests/integration/test_evaluation_recovery.py` (FR-014, FR-015, FR-017, FR-022, FR-023, SC-005, SC-008, SC-010).

## Phase 3: User Story 1 — Automatic final adapter comparison (P1, MVP)

Goal: a declared training recipe produces automatic task/rubric base-adapter results. Independent test: complete one supported training run with two declared suites; inspect adapter results without manual evaluation submission; inject one evaluation failure and retain training success.

- [X] T025 [P] [US1] Write final adapter/trigger transaction and replay tests in `apps/coire-api/tests/integration/test_training_evaluation_triggers.py`, including no suites for v1, full-queue/disabled-admission obligations and separate evaluation/training outcomes (FR-004, FR-011, FR-023, SC-001, SC-011).
- [X] T026 [P] [US1] Write comparability mismatch and exact input-overlap tests in `apps/coire-api/tests/unit/test_evaluation_comparison.py` and `apps/coire-agent/tests/test_evaluation_contamination.py`, including same input/different completion and unavailable data (FR-005, FR-006, FR-016, SC-004).
- [X] T027 [US1] Resolve/freeze suite declarations in `apps/coire-api/src/coire_api/training/specs.py` and enqueue final triggers atomically in `training/adapters.py` with scheduler reconciliation in `coire_scheduler/evaluations.py`; never change successful training due to evaluation scores (FR-003, FR-004, FR-011, FR-023, FR-024, SC-001).
- [X] T028 [US1] Implement provenance compatibility and typed comparison projection in `apps/coire-api/src/coire_api/evaluation/comparison.py`, and consumed-through-update training-input exact contamination scanning in `apps/coire-agent/src/coire_agent/evaluation_contamination.py` using existing dataset grants/normalization (FR-005, FR-006, FR-016, FR-019, SC-004).
- [X] T029 [US1] Expose final group/result links on training/adapter views and read-only comparison endpoints in `apps/coire-api/src/coire_api/console/training.py`, `training/adapters.py` and `routes/admin_evaluation_runs.py`; preserve harness evaluation_id meaning (FR-007, FR-011, FR-018).
- [X] T030 [US1] Generate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts` and add typed evaluation/group/SSE wrappers in `apps/coire-web/src/api/evaluations.ts` with `evaluations.test.ts` (FR-005, FR-007, FR-018, FR-020).
- [X] T031 [US1] Add accessible score/provenance/comparison components in `apps/coire-web/src/components/evaluations/Comparison.tsx` and `EvaluationDetail.tsx`; integrate `training/AdapterPanel.tsx` and `TrainingRun.tsx` with separate evaluation streams after training ends (FR-007, FR-018, FR-027, SC-001).
- [X] T032 [US1] Add opt-in suite selection/versioned form serialization in `apps/coire-web/src/components/training/TrainingForm.tsx` and a bounded `recipes/training/sft-evaluated.yaml` template; preserve old loss controls and v1 recipes (FR-003, FR-004, FR-024, SC-011).
- [X] T033 [US1] Test pending/failed/non-comparable/contaminated/expired evidence states, keyboard labels, unchanged verification and reconnect/reset in `apps/coire-web/src/components/evaluations/Comparison.test.tsx` and `training/TrainingForm.test.tsx` (FR-006, FR-007, FR-011, FR-016, FR-018, FR-027, SC-012).
- [x] T034 [US1] Add isolated tiny-model automatic final evaluation coverage in `tests/integration/test_evaluation_training.py`, proving base/adapter identity, one task/rubric group and no core suite execution (FR-004, FR-021, FR-029, SC-001).
- [x] T035 [US1] Record US1 automated evidence and remaining real-Studio gates in `specs/017-evaluation-verbs/execution-record.md`; verify the adapter comparison through the console using the test fixture and independently reproduce an infrastructure failure (FR-007, FR-014, FR-029, SC-001, SC-005).

## Phase 4: User Story 2 — On-demand evaluation (P1)

Goal: admin CLI/console can execute and inspect all suite types without a training job. Independent test: run harness/task/judge against ready base and adapter targets, cancel/rerun, and verify durable history plus exact verification behavior.

- [x] T036 [P] [US2] Write submit/list/detail/cancel/rerun/evidence/comparison API tests in `apps/coire-api/tests/contract/test_evaluation_runs_api.py`, including concurrent requests, unauthorized reads, retired targets and stale versions (FR-002, FR-017, FR-020, FR-022, FR-027).
- [X] T037 [P] [US2] Write legacy positional harness and new evaluation verb tests in `apps/coire-api/tests/unit/test_evaluation_cli.py`, covering measured engine identity, wait/no-wait, exit statuses and idempotent transport retry (FR-002, FR-026, SC-002).
- [X] T038 [P] [US2] Extend exact verification and loose-score rejection tests in `apps/coire-api/tests/unit/test_harness_evaluations.py`, `test_exact_target_verification.py` and `contract/test_admin_evaluations.py`; task/judge must never update the gate (FR-010, FR-011, FR-026).
- [X] T039 [US2] Implement manual submit/rerun/cancel operations in `apps/coire-api/src/coire_api/evaluation/service.py` and `routes/admin_evaluation_runs.py`, preserving fresh IDs versus idempotent replay and durable audit/owner attribution (FR-002, FR-017, FR-020, FR-022, SC-008).
- [X] T040 [US2] Restrict legacy score submission in `apps/coire-api/src/coire_api/routes/admin_evaluations.py` and feed only complete execution-bound harness evidence through `evaluations.py`; keep legacy GET/history and previous infrastructure-failure verification semantics (FR-010, FR-011, FR-026).
- [X] T041 [US2] Replace local CLI suite execution with durable submit/wait and add catalog/task/judge/history/compare/cancel/rerun/measurement verbs in `apps/coire-api/src/coire_api/cli.py`, retaining positional harness variant/adapter syntax and treating engine-version as an assertion (FR-002, FR-019, FR-026, SC-002).
- [X] T042 [US2] Add generated-type submission/history/cancel/rerun UI in `apps/coire-web/src/components/evaluations/EvaluationForm.tsx` and `EvaluationHistory.tsx`, linking existing model/training admin surfaces via `apps/coire-web/src/main.tsx`, `pages/Training.tsx` and `src/api/evaluations.ts` (FR-002, FR-018, FR-020, FR-022).
- [X] T043 [US2] Test CLI/console parity, access refusal, duplicate-submit handling, cancellation and retired-target history in `apps/coire-web/src/components/evaluations/EvaluationHistory.test.tsx` and `EvaluationForm.test.tsx` (FR-002, FR-017, FR-018, FR-020, SC-002).
- [X] T044 [US2] Prove unchanged registered-dataset analyze behavior and private/unverified evaluation without user WRITE access in `apps/coire-api/tests/unit/test_training_cli.py` and `tests/integration/test_training_targets.py` (FR-002, FR-010, FR-019, FR-020).
- [x] T045 [US2] Exercise all three on-demand suite types, fresh rerun and immutable old digest in `tests/integration/test_evaluation_suites.py`, proving the CLI performs no local harness execution (FR-001, FR-002, FR-017, FR-021, FR-029, SC-002, SC-008).

## Phase 5: User Story 3 — Checkpoint evaluation and resume (P2)

Goal: two declared pre-final boundaries evaluate safely and resume the exact full checkpoint. Independent test: supported training with two boundaries, scheduler restart and newer admin pause/cancel, including both-rank stop on the supported distributed configuration.

- [X] T046 [P] [US3] Write v1/v2 checkpoint-commit acknowledgment and stale-fence contract tests in `apps/coire-api/tests/contract/test_training_evaluation_checkpoints.py` and `apps/coire-node/tests/contract/test_training_evaluation_control.py` (FR-003, FR-013, FR-024).
- [X] T047 [P] [US3] Write trigger/pin/quota/replay and admin/protective-intent race tests in `apps/coire-api/tests/integration/test_training_evaluation_boundaries.py`, including node loss and failed-stop holds (FR-003, FR-012, FR-013, FR-017, FR-023, SC-009).
- [X] T048 [US3] Commit checkpoint evaluation trigger/pin/pause intent atomically under the job lock in `apps/coire-api/src/coire_api/training/checkpoints.py` and expose the versioned decision through existing internal checkpoint routes (FR-003, FR-013, FR-023).
- [X] T049 [US3] Consume and broadcast the fenced evaluation pause decision at the acknowledged completed-update boundary in `apps/coire-node/src/coire_node/training/worker.py` and `checkpoints.py`; prove all ranks stop at the common full checkpoint (FR-003, FR-013, FR-024, SC-009).
- [X] T050 [US3] Implement evaluation pause ownership, cleanup-before-resume and newer-intent precedence in `apps/coire-api/src/coire_scheduler/training_controller.py`, `evaluations.py` and `coire_api/training/service.py`; require live authority and fresh ordinary training admission (FR-012, FR-013, FR-023, SC-009).
- [X] T051 [US3] Reuse extraction/replication/serving-smoke primitives for private internal checkpoint adapters in `apps/coire-api/src/coire_api/training/adapters.py` and `runtime.py`, hiding evaluation-only adapters from user selection/publishing and retiring only owned artifacts (FR-003, FR-005, FR-010, FR-013).
- [X] T052 [US3] Integrate evaluation checkpoint pins and bounded backpressure into `apps/coire-api/src/coire_api/training/retention.py` and `coire_scheduler/training_retention.py`, retaining existing checkpoint count/byte quotas and releasing pins only after safe cleanup (FR-012, FR-013, FR-017, FR-027).
- [X] T053 [US3] Add attempt/update-aware checkpoint score and pause-owner presentation in `apps/coire-web/src/components/training/Checkpoints.tsx`, `TrainingRun.tsx` and `src/api/evaluations.ts`, using durable group streams (FR-003, FR-013, FR-018).
- [X] T054 [US3] Test checkpoint points, rolled-back attempts, reset/reconnect and operator override presentation in `apps/coire-web/src/components/training/CheckpointEvaluations.test.tsx` (FR-003, FR-013, FR-018, SC-009).
- [x] T055 [US3] Extend `tests/integration/test_evaluation_training.py` and `apps/coire-node/tests/engine/test_training_resume.py` for exact checkpoint evaluation/resume with restart and cancellation; preserve optimizer/RNG/sampler restoration (FR-003, FR-013, FR-023, FR-029, SC-009).
- [x] T056 [US3] Test old-node capability rejection before launch, old v1 continuation and drained binary rollback in `apps/coire-api/tests/integration/test_evaluation_compatibility.py` with unchanged historical recipe/resolved/manifests (FR-024, FR-030, SC-011).

## Phase 6: User Story 4 — Judge integrity and refusal (P2)

Goal: judge self-identity and failures are explicit and auditable. Core refusal/no-tools enforcement is already required in Foundation before any judge runs. Independent test: alias/variant/adapter self-judge matrix refuses before launch, while a distinct judge produces recorded rubric and counterbalanced pairwise results.

- [X] T057 [P] [US4] Complete model-ID/base-artifact alias, variant and adapter self-judge rejection matrix in `apps/coire-api/tests/unit/test_evaluation_authorization.py`, asserting no queued worker or resource side effects (FR-008, FR-009, FR-020, SC-003).
- [X] T058 [P] [US4] Add adversarial candidate instructions, no-tools/undeclared-target checks, reversed pair order, malformed-output retry exhaustion and evidence expiry tests in `apps/coire-agent/tests/test_evaluation_judge.py` (FR-008, FR-014, FR-015, FR-025, FR-027, SC-005, SC-007).
- [X] T059 [US4] Complete exact judge/rubric/order provenance and refusal explanations in `apps/coire-web/src/components/evaluations/EvaluationDetail.tsx` and `EvaluationForm.tsx`, distinguishing preference counts from independent quality scores (FR-005, FR-007, FR-009, FR-018, FR-025, SC-007).
- [x] T060 [US4] Test self-judge refusal/audit, unavailable judge, partial results and complete provenance over the admin API in `apps/coire-api/tests/contract/test_evaluation_judge_api.py`; no numeric aggregate for failed runs (FR-005, FR-009, FR-014, FR-020, SC-003, SC-005, SC-007).
- [x] T061 [US4] Extend `tests/integration/test_evaluation_suites.py` with genuinely distinct tiny candidate/judge artifacts and asserted actual generation/provenance, documenting tiny-model quality limits and the real-judge release gate (FR-008, FR-025, FR-029, SC-007).

## Phase 7: Cross-cutting release gates

All four stories are required for release. Implementation is authorized by the current speckit-implement request; all release gates remain required.

- [x] T062 Add end-to-end owner demotion/revocation, foreign evidence injection, retirement/history and cleanup isolation regressions in `apps/coire-api/tests/integration/test_evaluation_security.py` and `tests/integration/test_run_core_isolation.py` (FR-020, FR-021, FR-022, FR-026, FR-027, SC-010).
- [X] T063 Add jobs dashboard evaluation panels in `deploy/observability/grafana/dashboards/jobs.json`, baseline rules in `deploy/observability/alerts/evaluations.yaml` and rule tests in `deploy/observability/tests/evaluations.test.yaml`, with diagnostics-disabled history/alerts checks (FR-028, SC-012).
- [X] T064 Document status/kill/evidence retention/legacy CLI transition/drained rollback in `docs/runbooks/evaluations.md` and `docs/runbooks/sft-training.md`; update `docs/ARCHITECTURE.md`, `docs/design/DESIGN.md` and 017 status in `docs/ROADMAP.md` only to match delivered behavior (FR-026, FR-027, FR-030, SC-012).
- [X] T065 Document and wire default-off evaluation settings and existing private-volume namespace in `deploy/compose/README.md` and `deploy/compose/compose.yaml`; add configuration tests in `packages/coire-core/tests/test_evaluation_settings.py` without widening networks/mount audiences (FR-012, FR-015, FR-027, FR-030).
- [x] T066 Ensure isolated Mac tiny-model and container integration coverage in `.github/workflows/ci.yml` includes new evaluation tests and preserves every existing lint/type/OpenAPI/image-policy/scan/SBOM gate; CI must not target real Studios (FR-021, FR-029).
- [X] T067 Regenerate/check `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`, run full applicable lint/type/unit/contract/Postgres/tiny-model/web/image/rule checks from `specs/017-evaluation-verbs/quickstart.md`, and record results in `execution-record.md` without accepting required skips/failures (FR-024, FR-029).
- [X] T068 Run authorized real-Studio automatic final task/rubric comparisons and two declared checkpoint resumes, including scheduler restart and the supported two-rank boundary plus newer admin pause/cancel; record measured evidence references in `specs/017-evaluation-verbs/execution-record.md` (FR-003, FR-004, FR-013, FR-023, FR-029, SC-001, SC-009).
- [X] T069 Qualify real evaluation/chat coexistence, stale-profile/guard behavior, reachable cancellation ≤5s and safe drained rollback with diagnostics disabled; retain private receipts and measurements referenced from `specs/017-evaluation-verbs/execution-record.md` (FR-012, FR-022, FR-028, FR-030, SC-006, SC-010, SC-012).
- [x] T070 Review every requirement against actual evidence, update completion checkboxes and implementation handoff in `specs/017-evaluation-verbs/tasks.md` and `handoff.md`, and prepare the implementation PR description using `.github/pull_request_template.md` with Principles I–VII and actual CI/acceptance results (FR-029, FR-030).

## Dependencies and execution order

```text
Setup → Foundation → US1 final comparison → US2 on-demand → US3 checkpoints → US4 judge review → release gates
                     ↘ US2 can be developed after Foundation using standalone targets
                     ↘ US4 adversarial tests can be drafted after Foundation
US3 depends on US1 v2 resolution/final-group projections and the shared executor.
```

The linear order above is the default single-agent sequence. US1 is independently demonstrable after Foundation; it does not require manual CLI execution from US2. Foundation includes mandatory self-judge rejection, private-target authorization and judge no-tools constraints; US4 never defers those protections. US2 is independently testable without a training job. US3 tests can use prepared training fixtures, but implementation depends on shared US1 schedule parsing. Final release requires every phase, not only the MVP.

Within Foundation, contract tests precede models, models precede migration/transport, node preparation and agent scoring precede the run executor, and the executor precedes scheduler/admission/API integration. Shared files (`db.py`, `cli.py`, scheduler/controller, generated schemas) have one editor at a time. Within stories, the opening `[P]` test tasks can run together; subsequent implementation is sequential unless newly proven independent.

## Parallel examples

- Foundation: wire contract tests and frozen-v1 document tests touch different files after baseline fixtures exist.
- US1: transaction/replay tests and comparison/contamination tests can be drafted together after Foundation.
- US2: route tests, CLI tests and legacy verification tests form an independent test batch after the common contracts are stable.
- US3: checkpoint acknowledgment contract tests and database boundary-race tests form an independent batch after US1.
- US4: self-identity authorization matrix and agent adversarial-output tests are independent after Foundation.

These are task dependency opportunities, not permission to use agents outside the applicable session instructions.

## Implementation strategy

1. Complete setup and shared foundations with preserved v1 digests and Studio-only execution.
2. Demonstrate US1 automatic final task/rubric comparison as the MVP. A demonstration is not release acceptance.
3. Add and validate US2 on-demand, US3 checkpoint ownership and US4 judge integrity; preserve earlier tests.
4. Run the full release gates, real Studio acceptance and rollback measurements. Stop on a concrete missing prerequisite and record it; do not substitute mocks for real acceptance.
5. Keep private evidence outside Git and produce a reviewable PR with spec/constitution/evidence links. Merge/publish only under the next session's authorization and required protections.

## Requirement coverage index

Each entry identifies implementation and/or direct validation tasks. Full semantic coverage is checked by the final read-only analysis.

| Requirement | Tasks |
|---|---|
| FR-001 | T003, T005, T007, T008, T015, T020, T021, T045 |
| FR-002 | T010, T016, T021, T036, T037, T039, T041, T042, T043, T044, T045 |
| FR-003 | T004, T005, T027, T032, T046, T047, T048, T049, T051, T053, T054, T055, T068 |
| FR-004 | T004, T025, T027, T032, T034, T068 |
| FR-005 | T003, T005, T016, T017, T020, T026, T028, T030, T051, T059, T060 |
| FR-006 | T026, T028, T033 |
| FR-007 | T029, T030, T031, T033, T035, T059 |
| FR-008 | T010, T015, T017, T057, T058, T061 |
| FR-009 | T003, T010, T057, T059, T060 |
| FR-010 | T014, T015, T016, T038, T040, T044, T051 |
| FR-011 | T025, T027, T029, T033, T038, T040 |
| FR-012 | T018, T019, T047, T050, T052, T065, T069 |
| FR-013 | T046, T047, T048, T049, T050, T051, T052, T053, T054, T055, T068 |
| FR-014 | T014, T015, T017, T024, T035, T058, T060 |
| FR-015 | T003, T005, T006, T014, T015, T017, T018, T019, T023, T024, T058, T065 |
| FR-016 | T026, T028, T033 |
| FR-017 | T008, T009, T011, T017, T020, T021, T024, T036, T039, T043, T045, T047, T052 |
| FR-018 | T029, T030, T031, T033, T042, T043, T053, T054, T059 |
| FR-019 | T028, T041, T044 |
| FR-020 | T006, T010, T012, T016, T020, T021, T030, T036, T039, T042, T043, T044, T057, T060, T062 |
| FR-021 | T007, T012, T013, T014, T015, T016, T034, T045, T062, T066 |
| FR-022 | T023, T024, T036, T039, T042, T062, T069 |
| FR-023 | T008, T017, T023, T024, T025, T027, T047, T048, T050, T055, T068 |
| FR-024 | T001, T004, T005, T006, T008, T009, T012, T013, T027, T032, T046, T049, T056, T067 |
| FR-025 | T003, T007, T014, T015, T058, T059, T061 |
| FR-026 | T008, T016, T037, T038, T040, T041, T062, T064 |
| FR-027 | T011, T012, T013, T022, T023, T031, T033, T036, T052, T058, T062, T064, T065 |
| FR-028 | T020, T021, T022, T063, T069 |
| FR-029 | T002, T034, T035, T045, T055, T061, T066, T067, T068, T070 |
| FR-030 | T002, T006, T023, T056, T064, T065, T069, T070 |
| SC-001 | T025, T027, T031, T034, T035, T068 |
| SC-002 | T037, T041, T043, T045 |
| SC-003 | T010, T057, T060 |
| SC-004 | T026, T028 |
| SC-005 | T014, T024, T035, T058, T060 |
| SC-006 | T018, T019, T069 |
| SC-007 | T058, T059, T060, T061 |
| SC-008 | T009, T024, T039, T045 |
| SC-009 | T047, T049, T050, T054, T055, T068 |
| SC-010 | T024, T062, T069 |
| SC-011 | T001, T004, T009, T025, T032, T056 |
| SC-012 | T033, T063, T064, T069 |

**Generated counts**: 70 tasks; US1 11, US2 10, US3 11, US4 5; setup/foundation/release 33. Completion is tracked per task above.
