# Tasks: Preference Optimisation and Feedback Capture

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts](contracts/), [quickstart.md](quickstart.md).
**Branch**: `feat/018-preference-optimisation` | **Baseline**: `d2e4bbf` | **Status**: Implementation code complete; release withheld at failed T074. Checked tasks have recorded evidence.

Tests are required by the spec and constitution. Write behavior/contract tests before their corresponding implementation. New files listed below are intentional. Task IDs are sequential; `[P]` marks independent files/work after the stated phase prerequisites, not permission to bypass shared-file dependencies.

## Phase 1 — Setup

Establish the immutable baseline and test evidence before changing behavior.

- [X] T001 Create `specs/018-preference-optimisation/execution-record.md` with baseline d2e4bbf, supported matrix, prerequisite inventory, no-runtime-evidence status and mandatory gate slots; record explicit missing assets instead of enabling unsupported work. Maps FR-028, SC-012.
- [X] T002 [P] Freeze v1/v2 recipe/resolved/checkpoint/profile fixtures and existing SFT parse/hash/restore expectations in `packages/coire-core/tests/unit/test_preference_compatibility.py` and `apps/coire-api/tests/unit/test_training_specs.py`. Maps FR-003, SC-010.
- [X] T003 [P] Write `docs/adr/0014-preference-training-boundaries.md` documenting unchanged upstream trainer hooks, no new dependencies, objective-specific references, single-node support and preservation of existing engine ownership. Maps FR-006, FR-025.

## Phase 2 — Foundational contracts, privacy and data

Blocking foundation for every story. Privacy enforcement, capture-setting API and purge ownership must exist before any contribution surface is exposed. Tests precede their implementation.

- [X] T004 [P] Add strict feedback/preference contract tests for bounds, identity, roles, source precedence, objective options and legacy serialization in `packages/coire-core/tests/unit/test_feedback_models.py` and `test_preference_models.py`. Maps FR-001, FR-003, FR-011, FR-014, FR-026.
- [X] T005 Define feedback/settings/provenance/pair/export/lineage wire models and typed errors in `packages/coire-core/src/coire_core/models/feedback.py` and `packages/coire-core/src/coire_core/errors.py` per both contracts. Maps FR-008, FR-009, FR-011, FR-013, FR-015, FR-018, FR-022.
- [X] T006 Add preference rows/split/analysis/batch/metrics/value models in `packages/coire-core/src/coire_core/models/preference.py`; add distinct v3 intent/resolved/resource/checkpoint/acknowledgement unions in `models/training.py` and `models/training_node.py`, additive adapter objectives and lineage types in `models/adapters.py`, preserving old hashes. Maps FR-001, FR-003, FR-005, FR-006, FR-023, FR-024, SC-010.
- [X] T007 [P] Add seeded migration upgrade/drained-downgrade and SQL uniqueness/tombstone/active-pair constraint tests in `apps/coire-api/tests/integration/test_preference_migration.py`. Maps FR-011, FR-012, FR-019, FR-023, SC-010.
- [X] T008 Implement one reversible `apps/coire-api/alembic/versions/0033_preference_feedback.py` and corresponding `apps/coire-api/src/coire_api/db.py` rows/indexes; preserve historical SFT/evaluation JSON and nonblocking chat-purge provenance links. Maps FR-003, FR-005, FR-011, FR-012, FR-015, FR-018, FR-019.
- [X] T009 [P] Add live owner/admin/key/origin, content-free refusal audit, setting generation, replay and lock-order race tests in `apps/coire-api/tests/contract/test_feedback_settings.py` and `apps/coire-api/tests/integration/test_feedback_withdrawal.py`. Maps FR-016, FR-018, FR-019, FR-022, SC-004, SC-007.
- [X] T010 Implement owner capture-generation and live admin authorization, consistent owner/conversation/judgement locking, idempotent mutation receipts and GET/PATCH capture setting in `apps/coire-api/src/coire_api/feedback/eligibility.py`, `feedback/service.py` and `routes/chat_feedback.py`; register through `app.py`. Maps FR-012, FR-016, FR-018, FR-022.
- [X] T011 Implement withdrawal/deletion invalidation and bounded 24-hour body/replay/provenance purge in `apps/coire-api/src/coire_api/feedback/retention.py`, `chat/service.py`, `chat/maintenance.py` and `apps/coire-api/src/coire_scheduler/feedback.py`; keep cleanup active when admissions are disabled. Maps FR-016, FR-018, FR-019, FR-026, SC-004, SC-007.
- [X] T012 Extend counted private staging/quota and orphan reconciliation in `apps/coire-api/src/coire_api/training/storage.py`, `training/quota.py` and `feedback/exports.py`; enforce pair/source/export limits and keep holds until physical cleanup proof. Maps FR-015, FR-016, FR-026.
- [X] T013 [P] Add independent canonicalization/grouped split/cross-source overlap and both-answer validation tests in `packages/coire-core/tests/unit/test_preference_data.py`, preserving all SFT split fixtures. Maps FR-001, FR-002, FR-010, FR-021, SC-008, SC-010.
- [X] T014 Implement versioned preference normalization/prompt-group hashing and deterministic whole-group splits in `packages/coire-core/src/coire_core/preference_data.py`; extend dataset format dispatch and mixture leakage checks in `models/datasets.py`, `training_data.py` and `apps/coire-api/src/coire_api/training/mixtures.py` without rewriting SFT identities. Maps FR-001, FR-002, FR-021.
- [X] T015 [P] Add uploaded preference dataset and node analysis route contracts covering unsupported content, both-side diagnostics, exact template identities and no-core-tokenization in `apps/coire-api/tests/contract/test_preference_datasets.py` and `apps/coire-node/tests/contract/test_preference_analysis.py`. Maps FR-001, FR-002, FR-025, FR-028, SC-008.
- [X] T016 Extend dataset registration/storage and scheduler analysis dispatch in `apps/coire-api/src/coire_api/training/datasets.py`, `routes/admin_datasets.py`, `apps/coire-api/src/coire_scheduler/datasets.py` and node `apps/coire-node/src/coire_node/training/analysis_worker.py` for preference readiness and small-sample diagnostics. Maps FR-001, FR-002, FR-021, FR-025, FR-028.
- [X] T017 Add default-off preference admission and bounded feedback storage/purge settings in `packages/coire-core/src/coire_core/settings.py` and `deploy/compose/README.md`; implement content-free spans/logs/bounded metrics helpers in `apps/coire-api/src/coire_api/feedback/telemetry.py`. Maps FR-026, FR-027, SC-012.

## Phase 3 — US1: Explicit comparisons become a dataset (P1)

Independent test: at least 20 explicit pairs export once into a validated private preference dataset; thumbs never synthesize pairs and only the selected answer enters future context. Production exposure also requires US3 disclosure/accessibility gates.

- [X] T018 [P] [US1] Add owner feedback/current-state/comparison/create/read/select/dismiss route contracts with replay, quota and cross-owner cases in `apps/coire-api/tests/contract/test_chat_feedback.py` and `test_chat_comparisons.py`. Maps FR-008, FR-009, FR-010, FR-011, FR-012, FR-022.
- [X] T019 [P] [US1] Add exact target/cold-load/provenance, context selection, SSE reconnect and duplicate-charge tests in `apps/coire-api/tests/integration/test_chat_comparisons.py` and `apps/coire-api/tests/unit/test_chat_comparison_context.py`. Maps FR-009, FR-010, FR-011, FR-012, SC-008.
- [X] T020 [P] [US1] Add export route and Postgres crash/publication/source-version/withdrawal/quota tests in `apps/coire-api/tests/contract/test_feedback_exports.py` and `apps/coire-api/tests/integration/test_feedback_export.py`. Maps FR-014, FR-015, FR-016, FR-021, FR-022, FR-026, SC-001, SC-007.
- [X] T021 [US1] Capture bounded exact opted-in text prompt/rendering/target provenance during native dispatch in `apps/coire-api/src/coire_api/chat/turns.py`, `chat/streaming.py`, `gateway/resolution.py`, `gateway/targets.py` and `gateway/execution.py`; populate target projections and pin variant through cold load; refuse legacy/ineligible comparisons. Maps FR-009, FR-011, FR-018, FR-025, SC-008.
- [X] T022 [US1] Implement current thumbs, tags, clearing/versioned audit and bounded conversation feedback read in `apps/coire-api/src/coire_api/feedback/service.py` and `routes/chat_feedback.py`; never derive pair labels from thumbs. Maps FR-008, FR-011, FR-012, FR-022, SC-001.
- [X] T023 [US1] Implement explicit comparison admission, frozen target/settings, fresh recorded sampling seed, owned single generation, usage and expiry in `apps/coire-api/src/coire_api/feedback/comparisons.py` using existing chat/gateway execution; do not overload failed-turn retry. Maps FR-009, FR-010, FR-011, FR-012, FR-026, SC-008.
- [X] T024 [US1] Implement active-answer mapping and typed comparison SSE/reset projection in `packages/coire-core/src/coire_core/models/chat.py`, `apps/coire-api/src/coire_api/chat/turns.py`, `chat/processing.py`, `chat/streaming.py` and `chat/service.py`; check generation before every persisted delta/terminal event. Maps FR-009, FR-010, FR-016, FR-018, SC-004, SC-008.
- [X] T025 [US1] Wire comparison create/detail/selection/dismiss routes in `apps/coire-api/src/coire_api/routes/chat_feedback.py`; selection changes context only before subsequent turns, dismissal/expiry/failure unlocks, later rejudgement changes feedback only. Maps FR-009, FR-010, FR-012, FR-022.
- [X] T026 [US1] Implement bounded deterministic source selection, source precedence, staging, final live eligibility/version validation and immutable dataset/provenance publication in `apps/coire-api/src/coire_api/feedback/exports.py`; include zero/small/oversize results and ≤3 rebuilds. Maps FR-014, FR-015, FR-016, FR-021, FR-026, SC-001, SC-007.
- [X] T027 [US1] Install the ID-only durable export workflow, one-active/100-queued admission, deadlines, cancellation and crash cleanup in `apps/coire-api/src/coire_scheduler/feedback.py`, `main.py` and `workers.py`; recheck live admin authority before publication. Maps FR-015, FR-016, FR-022, FR-026.
- [X] T028 [US1] Implement authenticated export submit/list/detail/cancel in `apps/coire-api/src/coire_api/routes/admin_feedback.py` and register in `app.py`, with private dataset links and no raw row/download projection. Maps FR-015, FR-016, FR-021, FR-022.
- [X] T029 [US1] Add API-backed `coire feedback export/exports/export-show/export-cancel` and contract tests in `apps/coire-api/src/coire_api/cli.py` and `apps/coire-api/tests/unit/test_feedback_cli.py`; accept typed request files and expose warnings/readiness. Maps FR-015, FR-021, FR-028.
- [X] T030 [US1] Regenerate OpenAPI/TS for the implemented feedback surfaces and add generated-type-only transport functions in `apps/coire-web/src/api/feedback.ts`, updating `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`. Maps FR-008, FR-009, FR-015, FR-028.
- [X] T031 [P] [US1] Add component/browser tests for thumbs, pending choice, selection-only context, reconnect/expiry, failure and export warnings in `apps/coire-web/src/components/chat/Comparison.test.tsx`, `FeedbackControls.test.tsx` and `apps/coire-web/src/components/feedback/ExportForm.test.tsx`. Maps FR-008, FR-009, FR-010, FR-015, FR-017, FR-021, SC-008.
- [X] T032 [US1] Implement thumb/comparison rendering and controls in `apps/coire-web/src/components/chat/FeedbackControls.tsx`, `Comparison.tsx`, `Message.tsx`, `MessageList.tsx` and `pages/Chat.tsx`; use shared event stream, preserve keyboard focus and wait for US3 before exposing capture in production. Maps FR-008, FR-009, FR-010, FR-017, SC-008.
- [X] T033 [US1] Implement export form/history and preference readiness/warnings in `apps/coire-web/src/components/feedback/ExportForm.tsx`, `ExportHistory.tsx`, `components/training/DatasetPanel.tsx` and `pages/Training.tsx` using generated API transport. Maps FR-015, FR-021, FR-028, SC-001.
- [X] T034 [US1] Run US1 export/reconnect/context acceptance and record counted rows, exact source identities, crash outcomes and test commands in `specs/018-preference-optimisation/execution-record.md`; do not mark privacy-dependent UI release complete until US3. Maps FR-001, FR-008, FR-009, FR-010, FR-015, SC-001, SC-008.

## Phase 4 — US2: Train DPO/ORPO from an exact initial policy (P1)

Independent test: use uploaded preference fixtures to qualify both objectives and parameterizations from bare/SFT starts, full-state resume and exact serving. No dependency on organic feedback volume.

- [X] T035 [P] [US2] Add independent FP64 scalar objective/gradient goldens and FP32 tests for DPO sums, ORPO means/zero weight/extremes, shifted masks and accumulation in `apps/coire-node/tests/engine/test_preference_loss.py` and `apps/coire-node/tests/unit/test_preference_math_reference.py`. Maps FR-003, FR-004, FR-006, FR-028, SC-002, SC-009.
- [X] T036 [US2] Implement small stable DPO/ORPO loss adapters with pair-count aggregation and frozen reference gradients in `apps/coire-node/src/coire_node/training/preference_loss.py`, retaining unchanged upstream train/evaluate loops. Maps FR-003, FR-004, FR-025, SC-002.
- [x] T037 [US2] Implement exact two-answer rendering, prefix/mask checks, token-identical rejection and deterministic pair sampling in `apps/coire-node/src/coire_node/training/preference_data.py`, `training/rendering.py` and `training/sampler.py`; preserve SFT execution. Maps FR-001, FR-002, FR-023, FR-025, SC-008, SC-009.
- [x] T038 [P] [US2] Add runtime initialization/reference/RNG/frozen-tensor and real full-process resume tests in `apps/coire-node/tests/engine/test_preference_runtime.py` and `test_preference_resume.py` for both objectives × LoRA/QLoRA × bare/parent starts. Maps FR-004, FR-005, FR-023, FR-028, SC-002, SC-009.
- [x] T039 [US2] Implement exact local parent validation, matching config/tensors, independent initial DPO reference and RNG-safe fresh/resume loading in `apps/coire-node/src/coire_node/training/preference_runtime.py` and `training/objectives.py`; ORPO loads no reference. Maps FR-004, FR-006, FR-023, FR-025, SC-002, SC-009.
- [x] T040 [US2] Extend native worker objective dispatch and metrics/probe integration in `apps/coire-node/src/coire_node/training/worker.py` and `training/telemetry.py`; count response tokens separately, preserve sampler/RNG/mode around post-update probes, and retain guards/checkpoint callbacks. Maps FR-003, FR-023, FR-024, FR-027, SC-009.
- [X] T041 [P] [US2] Add v3 prepare/lease/status/acknowledgement/extraction capability contracts and mismatch/refusal tests in `apps/coire-node/tests/contract/test_preference_lifecycle.py` and `packages/coire-core/tests/unit/test_preference_node_models.py`. Maps FR-003, FR-004, FR-006, FR-023, FR-028, SC-010.
- [X] T042 [US2] Implement v3 capability advertisement, negotiated commands/acknowledgements and version-aware supervisor checks in `apps/coire-node/src/coire_node/agent.py`, `routes/training.py`, `training/supervisor.py`, `training/lease_snapshot.py` and shared `models/training_node.py`. Maps FR-003, FR-006, FR-023, SC-010.
- [X] T043 [US2] Extend actual preference measurement dispatch and objective-aware fingerprints/envelopes in `apps/coire-node/src/coire_node/training/measurement.py`, `training/datasets.py`, `routes/training_measurements.py` and `apps/coire-api/src/coire_api/training/measurements.py`; include paired activations/reference/probes/checkpoint peaks. Maps FR-006, FR-007, FR-026, SC-006.
- [X] T044 [P] [US2] Add v3 recipe/measurement/profile identity, impossible-fit, initial-adapter pin and API control tests in `apps/coire-api/tests/contract/test_preference_training.py` and `apps/coire-api/tests/unit/test_preference_specs.py`. Maps FR-003, FR-004, FR-006, FR-007, FR-022, SC-010.
- [X] T045 [US2] Implement v3 resolution/preflight, explicit initial/reference identity, depth/config validation, new-only digest namespace and input retention pins in `apps/coire-api/src/coire_api/training/specs.py`, `training/input_grants.py`, `training/retention.py` and `routes/admin_training.py`. Maps FR-003, FR-004, FR-005, FR-006, FR-007, FR-023.
- [X] T046 [US2] Extend node full-state checkpoint manifests/restore and mirrored commit for pair sampler/objective/initial/reference identities in `apps/coire-node/src/coire_node/training/checkpoints.py`, `training/worker.py` and API `apps/coire-api/src/coire_api/training/checkpoints.py`; reject partial/digest-mismatched resume. Maps FR-023, SC-009, SC-010.
- [X] T047 [US2] Extend scheduler v3 admission/controller/recovery and measurements in `apps/coire-api/src/coire_scheduler/training.py`, `training_admission.py`, `training_controller.py`, `training_recovery.py` and `training_measurements.py`; retain serving pins, fresh profiles, training/image exclusion and unknown-liveness holds. Maps FR-006, FR-007, FR-023, FR-026, SC-006, SC-009.
- [X] T048 [P] [US2] Add v3 empty/declared suite and final/checkpoint obligation ownership tests in `apps/coire-api/tests/contract/test_preference_evaluations.py` and `apps/coire-api/tests/integration/test_preference_evaluation_triggers.py`. Maps FR-020, FR-024, SC-003, SC-010.
- [X] T049 [US2] Generalize feature 017 v2-only checks explicitly for evaluated v3 in `apps/coire-api/src/coire_api/evaluation/training.py`, `evaluation/authorization.py`, `evaluation/inputs.py` and `apps/coire-api/src/coire_scheduler/training.py`; preserve base/result semantics, committed pause pins and later-admin-command precedence. Maps FR-020, FR-023, FR-024, SC-003, SC-010.
- [X] T050 [US2] Extend standalone output extraction, immutable lineage projection and GET lineage route in `apps/coire-node/src/coire_node/training/extraction.py`, `apps/coire-api/src/coire_api/training/adapters.py` and `routes/admin_adapters.py`; keep new adapters private/unverified and final tensors independent of parent paths. Maps FR-005, FR-020, SC-002, SC-003.
- [X] T051 [P] [US2] Add lineage/retired-parent/exact-serving/new-adapter-verification tests in `apps/coire-api/tests/contract/test_preference_adapters.py` and `apps/coire-api/tests/integration/test_preference_training.py`, including no-core-model and complete cleanup checks. Maps FR-005, FR-020, FR-023, FR-025, FR-028, SC-002, SC-003.
- [x] T052 [US2] Add bounded preference metric persistence/event projections in `apps/coire-api/src/coire_api/training/events.py`, `apps/coire-api/src/coire_scheduler/training_metrics.py` and contracts in `models/preference.py`; separate post-update accuracy/margin from training loss without changing old SFT metric shapes. Maps FR-024, FR-027, FR-028.
- [X] T053 [P] [US2] Add generated-type form/lineage/metric and recipe parity tests in `apps/coire-web/src/components/training/TrainingForm.test.tsx`, `AdapterPanel.test.tsx` and `TrainingActivity.test.tsx`. Maps FR-004, FR-005, FR-024, FR-028.
- [X] T054 [US2] Regenerate v3 schemas and extend `apps/coire-web/src/components/training/TrainingForm.tsx`, `TrainingActivity.tsx`, `AdapterPanel.tsx` plus existing training API transport for objective/options/parent, approved matrix and durable metric/lineage views. Maps FR-004, FR-005, FR-006, FR-024, FR-028.
- [x] T055 [US2] Add registry-bound DPO/ORPO templates in `recipes/training/dpo.yaml` and `recipes/training/orpo.yaml`; extend `apps/coire-api/src/coire_api/cli.py` recipe validation/submission/lineage and tests without local model work. Maps FR-003, FR-004, FR-025, FR-028.
- [x] T056 [US2] Run the complete tiny train/resume/serve matrix and frozen v1/v2 regressions in `apps/coire-node/tests/engine/test_preference_resume.py`, `apps/coire-api/tests/integration/test_preference_training.py` and existing SFT/evaluation tests; record exact tolerances/outcomes in `specs/018-preference-optimisation/execution-record.md`. Maps FR-003, FR-020, FR-023, FR-024, FR-028, SC-002, SC-003, SC-009, SC-010.

## Phase 5 — US3: Understand and control feedback capture (P1)

Independent test: disable/delete during every writer and export boundary, confirm immediate exclusion and ≤24-hour purge, preserve published snapshots, and operate all controls by keyboard.

- [X] T057 [P] [US3] Extend `apps/coire-api/tests/integration/test_feedback_withdrawal.py` with streaming/delta/replay/review/publication races, re-enable old-generation rejection, post-publication analysis preservation and scheduler crash/disabled-admission cleanup cases. Maps FR-016, FR-018, FR-019, SC-004, SC-007.
- [X] T058 [P] [US3] Add persistent setting/disclosure/version/conflict/accessibility tests in `apps/coire-web/src/components/chat/FeedbackSettings.test.tsx` and `apps/coire-web/src/pages/Chat.test.tsx`, including disabled contribution controls and ordinary-chat preservation. Maps FR-017, FR-018, FR-019, SC-005.
- [X] T059 [US3] Implement visible accepted-policy disclosure, capture toggle and pending/conflict/error UX in `apps/coire-web/src/components/chat/FeedbackSettings.tsx`, `FeedbackControls.tsx`, `Comparison.tsx` and `pages/Chat.tsx`; use generated settings API and announce transitions. Maps FR-017, FR-018, FR-019, SC-005.
- [X] T060 [US3] Complete content-erasure and privacy-safe replay coverage across `apps/coire-api/src/coire_api/feedback/retention.py`, `feedback/service.py`, `chat/streaming.py` and `apps/coire-api/src/coire_scheduler/feedback.py`; remove any content from receipts/DBOS args/audit and preserve published membership only. Maps FR-016, FR-018, FR-019, FR-022, FR-026, SC-004, SC-007.
- [X] T061 [US3] Run keyboard-only, owner/API-key/service-token and full withdrawal deadline acceptance in `apps/coire-api/tests/integration/test_feedback_withdrawal.py`, authenticated feedback contract tests and `tests/browser/feedback_keyboard.py`; record exclusion counts, purge completion, preserved snapshot identity and accessible UI evidence in `specs/018-preference-optimisation/execution-record.md`. Maps FR-017, FR-018, FR-019, FR-022, SC-004, SC-005, SC-007.

## Phase 6 — US4: Admin pairwise review (P2)

Independent test: an eligible explicit pair can be reviewed/skipped with optimistic conflicts and owner eligibility; exports honor selected judgement source without rewriting chat.

- [X] T062 [P] [US4] Add review list/detail/judgement auth, owner-withdrawal, two-admin conflict, skip/revisit and source-priority contracts in `apps/coire-api/tests/contract/test_feedback_review.py`. Maps FR-013, FR-014, FR-016, FR-022.
- [X] T063 [US4] Implement bounded queue/filter/detail, shared versioned admin judgement and reviewer skip state in `apps/coire-api/src/coire_api/feedback/service.py` and `routes/admin_feedback.py`; audit and owner-generation checks commit with the decision. Maps FR-013, FR-014, FR-016, FR-022.
- [X] T064 [US4] Verify owner/admin/owner_preferred export selection and effective-judgement tag/date filtering in `apps/coire-api/src/coire_api/feedback/exports.py` and `apps/coire-api/tests/integration/test_feedback_export.py`; retain exact reviewer/source provenance. Maps FR-014, FR-015, SC-001.
- [X] T065 [P] [US4] Add keyboard, skip/revisit, stale conflict and withdrawal-during-review tests in `apps/coire-web/src/components/feedback/ReviewQueue.test.tsx`. Maps FR-013, FR-017, SC-005.
- [X] T066 [US4] Regenerate review API types and implement queue/detail/decision UX in `apps/coire-web/src/components/feedback/ReviewQueue.tsx`, `api/feedback.ts` and `pages/Training.tsx`; recheck current state after conflicts and expose source provenance. Maps FR-013, FR-014, FR-017, FR-028.
- [X] T067 [US4] Run concurrent-review and opposing owner/admin choice acceptance in `tests/integration/test_feedback_review.py`, proving no chat-context mutation or opt-out bypass; record results in `specs/018-preference-optimisation/execution-record.md`. Maps FR-013, FR-014, FR-016, FR-022, SC-001, SC-007.

## Phase 7 — Operational qualification and release

All four stories are required for full 018. Required native/manual gates must pass; default-off admission is not a substitute for evidence.

- [X] T068 [P] Add Jobs/feedback panels and baseline stuck-export/overdue-purge/training fault alerts with firing/recovery tests in `deploy/observability/grafana/dashboards/jobs.json`, `deploy/observability/alerts/feedback.yaml` and `deploy/observability/tests/feedback.test.yaml`; validate diagnostics-disabled operation. Maps FR-027, SC-012.
- [X] T069 [P] Write operational see/kill/purge/rollback procedures in `docs/runbooks/feedback.md` and `docs/runbooks/preference-training.md`; reconcile `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, existing training runbook and compose settings with the accepted withdrawal policy and implemented support matrix. Maps FR-019, FR-025, FR-026, FR-027, FR-028, SC-012.
- [X] T070 Add/run 10,000-record/10-reader bounded performance, export deadline/queue/quota and privacy-safe telemetry checks in `tests/integration/test_feedback_performance.py`; record p95, publication timing and environment in `specs/018-preference-optimisation/execution-record.md`. Maps FR-015, FR-022, FR-026, FR-027, SC-011, SC-012.
- [X] T071 Wire preference numerical/native matrix and integration coverage into `.github/workflows/ci.yml` using existing isolated Mac fixtures, `apps/coire-node/tests/engine/test_preference_worker.py` and `apps/coire-api/tests/integration/test_preference_training.py`; explicitly enabled missing prerequisites fail, CI never targets real Studios. Maps FR-028, SC-002, SC-009, SC-010, SC-012.
- [X] T072 Run authorized real-Studio DPO/ORPO × dense LoRA/affine QLoRA qualification with chained starts, complete adapter serving and memory/probe/checkpoint measurements; record exact profiles and receipts in `specs/018-preference-optimisation/execution-record.md` per `quickstart.md`. Maps FR-004, FR-005, FR-006, FR-020, FR-028, SC-002, SC-003, SC-006.
- [x] T073 Run real scheduler/node restart, checkpoint corruption/replication/retention, cancellation ≤5 seconds, guard stop and unknown-liveness hold acceptance; record proofs in `specs/018-preference-optimisation/execution-record.md` using `quickstart.md`. Maps FR-007, FR-023, FR-028, SC-009, SC-012.
- [ ] T074 Qualify exact preference/chat coexistence and training/image exclusion with ≥100 baseline and mixed requests per resident, zero failures/swap and specified p95 limits; record measured profiles and invalidation tests in `specs/018-preference-optimisation/execution-record.md`. Maps FR-006, FR-007, FR-028, SC-006.
- [x] T075 Rehearse drained binary rollback and isolated seeded schema downgrade, including export/compare/withdrawal cleanup and evaluation-owned pauses; record restored runtime identities and retained data behavior in `specs/018-preference-optimisation/execution-record.md`. Maps FR-003, FR-019, FR-023, FR-026, SC-010, SC-012.
- [x] T076 Run full required Ruff/mypy/pytest/web/OpenAPI/pin/image build/scan/SBOM checks from `specs/018-preference-optimisation/quickstart.md`; refresh `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`, document every result with no required failed/skipped gates in the execution record. Maps FR-028, SC-010, SC-012.
- [ ] T077 Reconcile completed tasks and exact qualified scope in `specs/018-preference-optimisation/spec.md`, `plan.md`, `tasks.md`, `handoff.md` and `execution-record.md`; prepare the implementation PR using `.github/pull_request_template.md`, linking 018 and principles I–VII, dependency statement and measured limitations. Maps FR-028, SC-012.

## Dependencies and checkpoints

- Setup precedes shared implementation. Within foundations: T004 → T005/T006; T007 → T008; T009 → T010/T011; T012 + T013 → T014; T015 → T016; T017 completes configured telemetry/admission. All foundations precede story implementation.
- US1 tests T018–T020 may run in parallel after foundations; exact provenance precedes comparison execution, active-answer projection precedes selection UI, export transactions precede scheduler/routes/UI. Shared chat files are sequential.
- US2 can proceed after foundations with uploaded preference fixtures. Loss/render/init precede worker integration; negotiated contracts precede v3 node commands; exact resolver/measurement/checkpoint dispatch precedes admission/recovery and suite obligations. Schema or worker edits touching the same files are sequential.
- US3 setting API/eligibility/purge foundation is mandatory before capture. Its complete disclosure/accessibility and adversarial privacy proof gate any US1 production exposure. US3 may be completed before US2; numerical order does not require waiting on independent training work.
- US4 needs US1 explicit pairs and shared privacy services. Its source-choice export tests use US1 publication; admin review never changes owner transcript state.
- Operational tasks require all affected story gates; CI and documentation can be prepared earlier, but real matrix/coexistence/rollback and final checks gate completion. Never check off runtime gates from design review.

## Parallel execution examples

- Foundation: T004 schema tests, T007 migration tests, T009 privacy tests and T013 normalization tests use separate files; run after the setup fixtures are ready.
- US1: T018 owner route tests, T019 context/replay tests and T020 export transaction tests are independent. After transport contracts, web tests can proceed while export CLI work is completed.
- US2: numerical oracle tests and lifecycle/API contract tests use separate files. Runtime implementation remains ordered around shared worker, contracts and checkpoint modules.
- US3: settings component tests and Postgres withdrawal race tests can run independently; integration proof follows both implementations.
- US4: review API and browser tests can run independently; queue service and export-source changes share services and must be coordinated.

## Implementation strategy

First deliver the foundation plus US1 and US3 as a privacy-complete feedback/export increment. Independently build US2 using deterministic uploaded fixtures and keep preference admission disabled until qualified. Then complete US4 review and the full end-to-end loop. All four stories and operational gates are required for feature 018; the MVP checkpoint is not feature completion. No task authorizes lowering existing quality/network/verification gates.

## Requirement coverage

| Requirement | Tasks |
|---|---|
| FR-001 | T004, T006, T013, T014, T015, T016, T034, T037 |
| FR-002 | T013, T014, T015, T016, T037 |
| FR-003 | T002, T004, T006, T008, T035, T036, T040, T041, T042, T044, T045, T055, T056, T075 |
| FR-004 | T035, T036, T038, T039, T041, T044, T045, T053, T054, T055, T072 |
| FR-005 | T006, T008, T038, T045, T050, T051, T053, T054, T072 |
| FR-006 | T003, T006, T035, T039, T041, T042, T043, T044, T045, T047, T054, T072, T074 |
| FR-007 | T043, T044, T045, T047, T073, T074 |
| FR-008 | T005, T018, T022, T030, T031, T032, T034 |
| FR-009 | T005, T018, T019, T021, T023, T024, T025, T030, T031, T032, T034 |
| FR-010 | T013, T018, T019, T023, T024, T025, T031, T032, T034 |
| FR-011 | T004, T005, T007, T008, T018, T019, T021, T022, T023 |
| FR-012 | T007, T008, T010, T018, T019, T022, T023, T025 |
| FR-013 | T005, T062, T063, T065, T066, T067 |
| FR-014 | T004, T020, T026, T062, T063, T064, T066, T067 |
| FR-015 | T005, T008, T012, T020, T026, T027, T028, T029, T030, T031, T033, T034, T064, T070 |
| FR-016 | T009, T010, T011, T012, T020, T024, T026, T027, T028, T057, T060, T062, T063, T067 |
| FR-017 | T031, T032, T058, T059, T061, T065, T066 |
| FR-018 | T005, T008, T009, T010, T011, T021, T024, T057, T058, T059, T060, T061 |
| FR-019 | T007, T008, T009, T011, T057, T058, T059, T060, T061, T069, T075 |
| FR-020 | T048, T049, T050, T051, T056, T072 |
| FR-021 | T013, T014, T016, T020, T026, T028, T029, T031, T033 |
| FR-022 | T005, T009, T010, T018, T020, T022, T025, T027, T028, T044, T060, T061, T062, T063, T067, T070 |
| FR-023 | T006, T007, T037, T038, T039, T040, T041, T042, T045, T046, T047, T049, T051, T056, T073, T075 |
| FR-024 | T006, T040, T048, T049, T052, T053, T054, T056 |
| FR-025 | T003, T015, T016, T021, T036, T037, T039, T051, T055, T069 |
| FR-026 | T004, T011, T012, T017, T020, T023, T026, T027, T043, T047, T060, T069, T070, T075 |
| FR-027 | T017, T040, T052, T068, T069, T070 |
| FR-028 | T001, T015, T016, T029, T030, T033, T035, T038, T041, T051, T052, T053, T054, T055, T056, T066, T069, T071, T072, T073, T074, T076, T077 |
| SC-001 | T020, T022, T026, T033, T034, T064, T067 |
| SC-002 | T035, T036, T038, T039, T050, T051, T056, T071, T072 |
| SC-003 | T048, T049, T050, T051, T056, T072 |
| SC-004 | T009, T011, T024, T057, T060, T061 |
| SC-005 | T058, T059, T061, T065 |
| SC-006 | T043, T047, T072, T074 |
| SC-007 | T009, T011, T020, T026, T057, T060, T061, T067 |
| SC-008 | T013, T015, T019, T021, T023, T024, T031, T032, T034, T037 |
| SC-009 | T035, T037, T038, T039, T040, T046, T047, T056, T071, T073 |
| SC-010 | T002, T006, T007, T013, T041, T042, T044, T046, T048, T049, T056, T071, T075, T076 |
| SC-011 | T070 |
| SC-012 | T001, T017, T068, T069, T070, T071, T073, T075, T076, T077 |

## Task totals

Total: 77. shared: 27, US1: 17, US2: 22, US3: 5, US4: 6. Release remains withheld at T074; checkbox state and execution-record evidence determine completion.
