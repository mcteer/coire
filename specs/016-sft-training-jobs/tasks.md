# Tasks: SFT Training Jobs

**Input**: `specs/016-sft-training-jobs/{spec.md,plan.md,research.md,data-model.md,quickstart.md,contracts/}`
**Branch**: `feat/016-sft-training-jobs`
**Status**: All implementation, supported runtime acceptance and final release tasks passed; see execution-record.md and handoff.md.
The user restored Studio A RDMA; both Studio ports are active.
Hostfile/runtime preflight findings are recorded in `execution-record.md`. Remaining tasks are unchecked.
**Tests**: Required by the specification and Constitution VII. Write meaningful boundary/numerical/
race tests before their implementation; never count missing prerequisites or skipped gates as passes.

## Format and execution rules

`- [ ] TNNN [P?] [USn?] Action with repository-relative file path`.
`[P]` means disjoint work can run together after listed prerequisites; it does not authorize starting
an implementation agent during this planning session. Default execution is sequential within a
phase. Shared contracts precede services; regenerate API/TS types with each contract change.
No commits, pushes or PR creation are implied by this task list; obtain the user's instruction.

Foundation includes the minimal dataset/analysis, safe admission and checkpoint primitives needed
to make the P1 training slice real. Later US3 adds the complete dataset-management/mixture experience;
US4 adds two-rank and measured mixed-workload admission. Until those pass, unsupported choices must
return capability-unavailable errors, not be silently accepted or presented as shipped.

## Phase 1 — Setup and evidence

- [X] T001 Verify baseline, current Alembic head and locked runtimes; create `specs/016-sft-training-jobs/execution-record.md` recording actual commands/results and prerequisites, without secrets or model bytes.
- [X] T002 Add synthetic text/prompt/tool fixtures, isolated Postgres/node test helpers and an explicitly offline <=1 GB `COIRE_TEST_MODEL` engine fixture in `tests/conftest.py`, `tests/fixtures/training/` and `apps/coire-node/tests/engine/conftest.py`; never download implicitly or target real Studios from CI.
- [X] T003 Record the bare trainer hooks, checkpoint commit, exact targets and Studio rendering decisions in `docs/adr/0012-sft-training-boundaries.md` (recheck next ADR number) with Constitution I–VII compliance and the fixed numerical acceptance tolerances.

## Phase 2 — Shared contracts and blocking foundations

### Contracts and persistence

- [X] T004 Write strict schema/compatibility tests for TrainingSpec, YAML intent, datasets, tool conversations, exact targets and node manifests in `packages/coire-core/tests/test_training_contracts.py`, including unknown fields, finite bounds, UUID compatibility and invalid pair selectors.
- [X] T005 Extend `packages/coire-core/src/coire_core/models/conversation.py` with optional bounded tools/metadata and tool-only assistant turns; validate call/response relationships without breaking existing chat/image messages.
- [X] T006 Add dataset revision/analysis/split/mixture and training-example models in `packages/coire-core/src/coire_core/models/datasets.py` using the data-model's deterministic identity and bounded diagnostic rules.
- [X] T007 Add versioned TrainingSpec, source/resolved intent, job/attempt/control/event/metric/measurement models in `packages/coire-core/src/coire_core/models/training.py`; only SFT/held-out loss and explicitly supported optimizer/parameterization options are accepted.
- [X] T008 Add adapter lifecycle, strict public selector and exact InferenceTarget models in `packages/coire-core/src/coire_core/models/adapters.py`; adapter identity never inherits base verification or caller paths.
- [X] T009 Add fenced node commands, full checkpoint manifests, process/stop proof and bounded transfer/dataset grant contracts in `packages/coire-core/src/coire_core/models/training_node.py`.
- [X] T010 Extend affected shared `gateway.py`, `chat.py`, `engine.py`, `instance.py`, `harness.py`, `runs.py`, `mcp.py`, `auth.py`, `console.py` and `node.py` under `packages/coire-core/src/coire_core/models/` with backward-compatible optional exact-target/training projections; default legacy credentials to base-only scope.
- [X] T011 Add typed training errors, default-off feature gating and every plan limit to `packages/coire-core/src/coire_core/errors.py` and `settings.py`; document the corresponding environment variables in `deploy/compose/README.md` as they are introduced.
- [X] T012 Write real-database migration/constraint tests in `apps/coire-api/tests/integration/test_training_migration.py`, including idempotency/name uniqueness, duplicate finalization, target-null legacy rows and downgrade refusal with live references.
- [X] T013 Implement training/dataset/analysis/attempt/participant/checkpoint/copy/adapter/event/metric/evidence persistence and nullable exact-target columns in `apps/coire-api/src/coire_api/db.py` and one reversible `apps/coire-api/alembic/versions/0031_sft_training.py` after rechecking the current head.
- [X] T014 Implement common admin/current-authority/origin guards, audited idempotent command transactions and version conflicts in `apps/coire-api/src/coire_api/training/authorization.py` and `service.py`; test mandatory audit rollback and exclusion of ops/run/node credentials.
- [X] T015 Implement durable event/loss persistence, typed replay/reset and content-free telemetry helpers in `apps/coire-api/src/coire_api/training/events.py` and `telemetry.py`, retaining attempt boundaries and bounded reads independent of diagnostics storage.
- [X] T016 Implement bounded safe YAML parsing, form/source equivalence and canonical intent/resolved digests in `apps/coire-api/src/coire_api/training/specs.py`; reject duplicate keys, aliases, custom tags, depth/size overflow, unknown fields and nonfinite values.

### Pinned runtime, checkpoint and data primitives

- [X] T017 Write failing pinned-runtime safety tests in `apps/coire-node/tests/engine/test_training_runtime.py` for offline local loading, `model_file`/remote code refusal, explicit target modules, frozen base and supported LoRA/QLoRA/DoRA configuration.
- [X] T018 Implement the safe local loader and SFT-only objective registry in `apps/coire-node/src/coire_node/training/objectives.py` using existing bare MLX/mlx-lm APIs; reject arbitrary imports, Hub resolution, unsupported matrices and upstream gradient-checkpoint monkeypatching.
- [X] T019 Write training-versus-actual-serving token/mask tests in `apps/coire-node/tests/engine/test_training_rendering.py`, including multipart text, tool-only turns, thinking settings, override content and padded/zero-target/overlength boundaries.
- [X] T020 Implement model-free canonical serialization in `packages/coire-core/src/coire_core/conversation_rendering.py` and shared upstream rendering use in `apps/coire-node/src/coire_node/training/rendering.py`; correct effective template-content binding in `coire_node/engines.py` and cover existing base serving.
- [X] T021 Implement explicit raw-text/final-assistant masked loss and deterministic stateful single-source batching in `apps/coire-node/src/coire_node/training/loss.py` and `sampler.py`, with no implicit truncation, dropped batch or prefetch beyond the saved cursor.
- [X] T022 Write checkpoint round-trip and corruption/negative-reset tests in `apps/coire-node/tests/engine/test_training_resume.py` for optimizer moments/step, schedule, MLX current key, sampler state and exact key/shape/dtype validation.
- [X] T023 Implement completed-update safetensors/JSON snapshot/restore, crash-durable directory/manifest commit and bounded retention primitives in `apps/coire-node/src/coire_node/training/checkpoints.py`; preserve the last complete recovery point and restore RNG on the training thread.
- [X] T024 Write control/data-listener, artifact grant/digest/Range/expiry/refresh/revoke and atomic-import contract tests in `apps/coire-node/tests/contract/test_training_artifacts.py`, including no control-fabric fallback or arbitrary host/path.
- [X] T025 Implement immutable checkpoint/adapter artifact stores and transfer routes in `apps/coire-node/src/coire_node/training/artifacts.py` and `routes/training_artifacts.py`, reusing declared peers/DataFabricClient with per-artifact grants and independently verified full copies.
- [X] T026 Write upload/analysis baseline API tests in `apps/coire-api/tests/contract/test_training_datasets.py` for all three row formats, quotas, invalid rows, auth/origin/audit, async readiness and private source-grant access.
- [X] T027 Implement private streamed source storage, complete-row schema validation, exact-duplicate grouped split manifests and upload/list/detail routes in `apps/coire-api/src/coire_api/training/storage.py`, `training/datasets.py` and `routes/admin_datasets.py`; hold aggregate disk quota before writing.
- [X] T028 Implement Studio CPU tokenizer analysis with accounted memory and timeout in `apps/coire-node/src/coire_node/training/datasets.py` and `routes/training.py`, plus durable dispatch and node-bound private source delivery in `apps/coire-api/src/coire_scheduler/datasets.py` and `coire_api/training/datasets.py`; analysis must load no base weights.
- [X] T029 Add local prepare/reservation and aggregate disk-hold accounting in `apps/coire-node/src/coire_node/training/journal.py`, `reservations.py` and `agent.py`, using the existing shared admission lock and immutable command digest comparisons.
- [X] T030 Implement conservative single-Studio full-envelope training admission in `apps/coire-api/src/coire_scheduler/training_admission.py` and `coire_api/placement/service.py`: initially no mixed accelerator workload, atomic protected pin/lease checks, counted holds and draining victim intents; include reserved preflight measurement rather than caller memory estimates.
- [X] T031 Register implemented contracts/routes and regenerate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts` through `coire_api/openapi.py`; verify base UUID and existing chat contract snapshots remain compatible.
- [X] T032 Run the offline loader/rendering/full-state compatibility gates from `specs/016-sft-training-jobs/quickstart.md` and record measured results in `execution-record.md`; stop for an actual unsupported hook rather than weaken recovery semantics.

**Checkpoint**: Shared types, safe runtime hooks, mirrored artifact primitives and a real validated
single-source training input exist. No public training capability is enabled merely by this checkpoint.

## Phase 3 — US1: Train and use an adapter (P1)

**Goal**: Complete the recipe/form -> training -> private ready adapter -> exact inference loop.
**Independent test**: One uploaded valid dataset and each supported parameterization; compare
recipe/form resolved settings, losses, actual selected adapter, independent verification and publication.

### Tests first

- [X] T033 [P] [US1] Add all training/adapter admin route contract tests in `apps/coire-api/tests/contract/test_training_api.py`, covering source/form equivalence, validation/recipes, queue/idempotency/name conflicts, authority and safe failures.
- [X] T034 [P] [US1] Add exact-target serving/evaluation/run-scope/failover contract tests in `apps/coire-api/tests/contract/test_adapter_targets.py`, including simultaneous base and two adapters and verified-base/unverified-adapter refusal.
- [X] T035 [P] [US1] Add the simulated end-to-end submission/finalization and publication race scenarios in `tests/integration/test_training_lifecycle.py` using real persisted jobs, node receipts and mirrored artifact verification.

### Runtime, services and serving

- [X] T036 [US1] Implement owned trainer spawn intent, explicit argv, PID/create-time journal, private control channel and process-group stop proof in `apps/coire-node/src/coire_node/training/supervisor.py` and `journal.py`; register via `agent.py`, never acquisition `resume_all()`.
- [X] T037 [US1] Implement bare training execution and typed progress in `apps/coire-node/src/coire_node/training/worker.py`: completed optimizer-update callbacks, independent held-out evaluation, finite losses, full state snapshots and scratch-only upstream adapter output.
- [X] T038 [US1] Implement transactional submission/preflight/queue/output-name reservation and recipe/validate/job/list/detail/metrics/events routes in `apps/coire-api/src/coire_api/training/service.py` and `routes/admin_training.py`; freeze source/resolved identities and current-authority provenance.
- [X] T039 [US1] Implement the durable single-node preflight/reserve/start/observe/finalize workflow in `apps/coire-api/src/coire_scheduler/training.py`, including finite queue/execution deadlines and baseline cancellation that never releases uncertain holds.
- [X] T040 [US1] Wire all typed analysis/training/artifact command polling and idempotent execution in `apps/coire-api/src/coire_api/nodes_client.py`, `training_executor.py` and `coire_scheduler/main.py`/`workers.py`; API handlers enqueue only.
- [X] T041 [US1] Implement final artifact extraction, exact-base compatibility, reserved inference smoke, both-copy verification and fenced final publication in `apps/coire-api/src/coire_api/training/adapters.py` and `checkpoints.py`; no tensor bytes on core or auto-verification.
- [X] T042 [US1] Add central exact target resolution in `apps/coire-api/src/coire_api/gateway/targets.py` and update `resolution.py`/`loading.py` to coalesce by target, exclude adapter instances for base requests and forbid base/smaller-variant fallback for a pair.
- [X] T043 [US1] Propagate target identity through instance creation/commands/placement/reconciliation in `apps/coire-api/src/coire_api/routes/instances.py`, `apps/coire-api/src/coire_api/instance/service.py`, `apps/coire-api/src/coire_scheduler/instances.py` and `placement.py`; adapter instances are single-node and never reused by parent-model lookup.
- [X] T044 [US1] Bind dedicated base-plus-adapter instance identity, registry-only `--adapter-path`, proxy rewrite, readiness and re-adoption in `apps/coire-node/src/coire_node/engines.py` and `routes/engines.py`; deduplication must not use base slug alone.
- [X] T045 [US1] Extend compatible chat/completions/messages and native picker/turn selection in `apps/coire-api/src/coire_api/routes/v1.py`, `gateway/anthropic.py` and `chat/service.py` for strict pair selectors and optional `coire_variant_id`, preserving external-provider/text/base behavior.
- [X] T046 [US1] Extend actual harness evaluation context, request routing and immutable scorecards in `apps/coire-api/src/coire_api/evaluations.py`, `routes/admin_evaluations.py` and `cli.py` for exact base/adapter targets; a pass/failure changes only that subject's verification.
- [X] T047 [US1] Enforce exact target grants and live write-verification checks in `apps/coire-api/src/coire_api/runs.py`, `run_tokens.py` and `run_executor.py`; base-only legacy tokens cannot select adapters or substitute another verified variant.
- [X] T048 [US1] Propagate target identity through `apps/coire-node/src/coire_node/runs.py`, `apps/coire-agent/src/coire_agent/__main__.py`, `gateway_model.py`, `harness.py` and `apps/coire-api/src/coire_mcp/tools.py`; transport must request the evaluated target while MCP remains exactly research/plan/apply.
- [X] T049 [US1] Exclude adapter engines from failover residency/base relay and refuse pair selectors in `apps/coire-node/src/coire_node/routes/failover.py`, `apps/coire-api/src/coire_api/failover/publication.py` and `apps/coire-failover/src/coire_failover/app.py`; test shared base-slug collision.
- [X] T050 [US1] Record exact target attribution and correct per-target load/list state in `apps/coire-api/src/coire_api/gateway/usage.py`, `routes/models.py`, `routes/v1.py` and console projections; warm adapters must not make base-only targets appear warm.
- [X] T051 [US1] Implement audited adapter list/detail/publication/unpublication/retirement routes in `apps/coire-api/src/coire_api/routes/admin_adapters.py` and `training/adapters.py`, including effective base entitlements, dependency revocation and artifact privacy.

### CLI and console

- [X] T052 [US1] Add installed argparse CLI `data upload/list/show`, `train recipes/validate/submit/list/show/events`, `adapter list/show/publish/unpublish/retire` and exact evaluation dispatch in `apps/coire-api/src/coire_api/cli.py`; cover API calls and exit behavior in `apps/coire-api/tests/unit/test_training_cli.py`.
- [X] T053 [US1] Add versioned LoRA/QLoRA/DoRA templates and required registry bindings in `recipes/training/sft-lora.yaml`, `sft-qlora.yaml` and `sft-dora.yaml`; seed console recipe responses without executable placeholder IDs.
- [X] T054 [US1] Add generated-type API calls and reconnectable event/metrics hook in `apps/coire-web/src/api/training.ts` and `hooks/useTrainingJob.ts` using shared `useEventStream`, with reset and attempt-boundary handling.
- [X] T055 [US1] Build admin-gated Training shell/runs rail/progress/loss/spec views in `apps/coire-web/src/pages/Training.tsx`, `components/training/TrainingRun.tsx`, `styles/training.css`, `App.tsx` and `components/AppShell.tsx` using existing design tokens.
- [X] T056 [US1] Build schema-backed recipe/form binding and source/resolved YAML preview in `apps/coire-web/src/components/training/TrainingForm.tsx` and `RecipePicker.tsx`, rejecting unsupported choices and displaying pending preflight/capacity clearly.
- [X] T057 [US1] Add adapter readiness/publication/verification views and exact chat selection in `apps/coire-web/src/components/training/AdapterPanel.tsx`, `components/chat/ModelPicker.tsx` and `api/chat.ts`; label feature 017 comparisons unavailable.
- [X] T058 [US1] Regenerate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts` and run all new API/node/target/CLI contracts with existing base-client regressions; record results in `specs/016-sft-training-jobs/execution-record.md`.
- [X] T059 [US1] Add and run form/picker/progress/error/permission/accessibility tests in `apps/coire-web/src/pages/Training.test.tsx` and `components/training/TrainingForm.test.tsx`, including source/form equality and ordinary-user denial.
- [X] T060 [US1] Prove all approved LoRA/QLoRA/DoRA parameterizations produce served adapters using `apps/coire-node/tests/engine/test_training_runtime.py`; preserve exact variant/adapter evidence and unsupported-matrix refusals in `execution-record.md`.
- [X] T061 [US1] Add real integration target propagation tests in `tests/integration/test_training_targets.py` across gateway, tokens, harness evaluation/results and failover, including post-pass isolation and failed reevaluation revocation.
- [X] T062 [US1] Integrate training activity, loss summary and existing admin Jobs kill visibility in `apps/coire-api/src/coire_api/console/service.py`, `apps/coire-api/src/coire_api/routes/admin_console.py`, `packages/coire-core/src/coire_core/models/console.py` and `apps/coire-web/src/App.tsx`; all new paths emit telemetry through training helpers.
- [X] T063 [US1] Execute the complete independent recipe/form -> adapter inference scenario in `specs/016-sft-training-jobs/quickstart.md` sections 4 and 6 and record actual results in `execution-record.md`; capability exposure stays limited to verified implemented modes.

## Phase 4 — US2: Recover, pause and stop training (P1)

**Goal**: Recover full progress safely and keep every attempt controllable across failure.
**Independent test**: Compare uninterrupted and interrupted trajectories; exercise agent/scheduler/
Studio restart, cancel/publish races, corrupted checkpoints and explicit promotion.

### Tests first

- [X] T064 [P] [US2] Add node restart/half-spawn/PID reuse/lease/fence/control protocol tests in `apps/coire-node/tests/contract/test_training_lifecycle.py`, including deadline and stop-proof semantics.
- [X] T065 [P] [US2] Add real-Postgres checkpoint-commit/cancel/finalize/idempotency races in `tests/integration/test_training_transactions.py`; use concurrent sessions rather than only mocked lock ordering.
- [X] T066 [P] [US2] Extend offline interruption tests in `apps/coire-node/tests/engine/test_training_resume.py` to three trials, nonzero dropout, accumulation, schedule changes, corruption fallback and pre-first-checkpoint restart.

### Implementation

- [X] T067 [US2] Implement fenced core checkpoint commitment after complete manifests/two copies and safe corruption fallback in `apps/coire-api/src/coire_api/training/checkpoints.py`, retaining immutable attempt/update lineage and no stale commit.
- [X] T068 [US2] Implement authoritative re-adoption/death/unknown reconciliation and recovery-generation selection in `apps/coire-api/src/coire_scheduler/training_recovery.py` and node `training/supervisor.py`; never treat core timeout as death proof.
- [X] T069 [US2] Implement 30 s renewable execution lease, <=1 s local watchdog, completed-update pause and <=5 s process-group cancel lane in `apps/coire-node/src/coire_node/training/guard.py`, `worker.py` and `routes/training.py`.
- [X] T070 [US2] Implement admin pause/resume/cancel state transitions and current authority/input/runtime rechecks in `apps/coire-api/src/coire_api/training/service.py`, `routes/admin_training.py` and `coire_scheduler/training.py`; admin pause stays manual, cancelled/failed jobs stay terminal.
- [X] T071 [US2] Implement safe release-after-stop, counted uncertain holds and version-aware eviction restoration in `apps/coire-api/src/coire_scheduler/training_recovery.py` and `training_admission.py`, preserving newer pin/retire/placement decisions.
- [X] T072 [US2] Implement complete retained checkpoint promotion with immutable serving-artifact retention in `apps/coire-api/src/coire_api/training/adapters.py` and `routes/admin_training.py`; cancellation blocks automatic output but permits a separate explicit audited promotion.
- [X] T073 [US2] Add control/checkpoint/promotion CLI verbs and terminal-state diagnostics in `apps/coire-api/src/coire_api/cli.py`, with command idempotency, optimistic versions and no silent replay of a changed action.
- [X] T074 [US2] Add pause/resume/stop, checkpoint chips/promotion, uncertainty/recovery and rolled-back loss segments in `apps/coire-web/src/components/training/TrainingControls.tsx`, `Checkpoints.tsx` and `TrainingRun.tsx`; test deadline fallback and SSE reconnection.
- [X] T075 [US2] Run lifecycle/transaction/node protocol suites and regenerate affected OpenAPI/TS artifacts; record three exact-state/numeric recovery comparisons and control timings in `specs/016-sft-training-jobs/execution-record.md` using quickstart sections 3 and 5.
- [X] T076 [US2] Exercise revoked authorization, runtime mismatch, peer-unavailable/corrupt newest checkpoint and core/node partitions in `tests/integration/test_training_lifecycle.py`; verify no leaked holds, duplicate trainers or late publication after reconciliation.
- [X] T077 [US2] Implement bounded checkpoint/job/staging retention and terminal delete commands in `apps/coire-api/src/coire_api/training/retention.py`, `routes/admin_training.py`, `coire_scheduler/training.py` and node `training/artifacts.py`; preserve live inputs, latest complete state and promoted artifacts.
- [X] T078 [US2] Add storage-full/reference/downgrade/cleanup failure tests in `apps/coire-api/tests/contract/test_training_retention.py` and `apps/coire-node/tests/unit/test_training_retention.py`; verify deletion receipts and observable unresolved cleanup.
- [X] T079 [US2] Add terminal job deletion and retained-artifact diagnostics to `apps/coire-api/src/coire_api/cli.py` and `apps/coire-web/src/components/training/TrainingControls.tsx` without hiding historical lineage referenced by adapters.
- [X] T080 [US2] Execute the independent restart/pause/cancel/promotion acceptance matrix from `specs/016-sft-training-jobs/quickstart.md` and record remaining concrete hardware prerequisites in `execution-record.md` without checking their final gates prematurely.

## Phase 5 — US3: Understand and reproduce datasets (P2)

**Goal**: Complete reusable analysis, deterministic mixtures and dataset lifecycle/console tooling.
**Independent test**: Upload/analyze each format; compare split/mixture identities and rejection
cases without training; then resume a mixture-backed run across an epoch boundary.

- [X] T081 [P] [US3] Add deterministic duplicate-group split and cross-dataset leakage/property tests in `apps/coire-api/tests/unit/test_training_datasets.py`, including seed zero, tiny splits and repeated analysis identities.
- [X] T082 [P] [US3] Add deterministic quota/weighted/sequential/replacement/rank sampler tests in `apps/coire-node/tests/unit/test_training_sampler.py`, including exact cursor restoration and no dropped incomplete global batch.
- [X] T083 [US3] Implement full index-based mixture compilation/preflight in `apps/coire-api/src/coire_api/training/datasets.py`, validating proportions/sample pools/largest-remainder quotas and duplicate split conflicts without merged corpus materialization.
- [X] T084 [US3] Extend `apps/coire-node/src/coire_node/training/sampler.py` and `datasets.py` with immutable multi-source indices, versioned independent RNG state and deterministic global batch assignment for later rank partitioning.
- [X] T085 [US3] Complete automatic and explicit tokenizer/template-specific analysis/reanalysis receipts, token histogram/role/duplicate reporting and analysis-failure retry in `apps/coire-api/src/coire_api/training/datasets.py` and `coire_scheduler/datasets.py`, without mutating prior results.
- [X] T086 [US3] Complete dataset analyze/status/delete/reference-blocking routes and private cache cleanup in `apps/coire-api/src/coire_api/routes/admin_datasets.py`, `training/retention.py` and node `training/datasets.py`; terminal provenance remains readable after purge.
- [X] T087 [US3] Add `coire data analyze --wait` and delete CLI behavior in `apps/coire-api/src/coire_api/cli.py`, including finite polling timeouts, immutable selected tokenizer identity and actionable row errors.
- [X] T088 [US3] Build dataset upload/analysis/provenance/retention and mixture editor panels in `apps/coire-web/src/components/training/DatasetPanel.tsx` and `MixtureEditor.tsx`, with exact source revisions and no remote-import controls.
- [X] T089 [US3] Add dataset/mixture UI and API tests in `apps/coire-web/src/components/training/DatasetPanel.test.tsx` and `apps/coire-api/tests/contract/test_training_datasets.py`, covering bounded errors, analysis pending/retry and active-reference deletion refusal.
- [X] T090 [US3] Verify every supported tool conversation renders before admission and metadata never enters supervision/telemetry in `apps/coire-node/tests/engine/test_training_rendering.py` and `apps/coire-api/tests/unit/test_training_datasets.py`.
- [X] T091 [US3] Run a mixture-backed interrupted tiny job across source/permutation boundaries in `apps/coire-node/tests/engine/test_training_resume.py`; prove exact sample order and optimizer continuation with the fixed tolerances.
- [X] T092 [US3] Regenerate schema/TS and execute the independent dataset/split/mixture acceptance in `specs/016-sft-training-jobs/quickstart.md` section 4, recording identities and test results in `execution-record.md`.
- [X] T093 [US3] Verify upload/analysis quotas, 30-minute timeout/cancel, aggregate core/node cache accounting and 24-hour orphan sweep in `tests/integration/test_training_datasets.py`; failed uploads/analyses cannot become ready or consume unbounded disk.
- [X] T094 [US3] Document supported row/loss formats, provenance, deterministic mixture semantics and input retention/deletion in `docs/runbooks/sft-training.md`, linked from the dataset UI's help text.

## Phase 6 — US4: Train within cluster capacity (P2)

**Goal**: Full atomic two-rank training and measured chat-first concurrency without swap.
**Independent test**: Competing admissions/pins/leases, two-rank failure/recovery, exact workload
measurement, protected pinned chat, guard deadlines and safe eviction restoration.

- [X] T095 [P] [US4] Add two-node reservation/pin/lease/eviction transaction race tests in `tests/integration/test_training_transactions.py`, requiring full per-node weights/optimizer memory and all-or-none starts.
- [X] T096 [P] [US4] Add profile fingerprint/multiplicity/expiry, per-target 100-completion measurement floors, 30-sample/5-minute live-window and 60-second freshness boundaries, bidirectional image/train exclusion and mixed admission tests in `apps/coire-api/tests/unit/test_training_admission.py`, including pinned ops residency and adapter identities.
- [X] T097 [US4] Extend atomic admission across both ordered node locks in `apps/coire-api/src/coire_scheduler/training_admission.py` and `coire_api/placement/service.py`, committing draining marks, full holds and restoration intent together; exclude sharded victims as protected occupancy.
- [X] T098 [US4] Include trainer footprint, unknown/stale health and physical overage in `apps/coire-api/src/coire_api/nodes_prober.py`, `placement/service.py`, node `metrics.py` and `training/supervisor.py`; preserve the >10% drift alert and no-swap headroom.
- [X] T099 [US4] Implement guarded memory/coexistence measurement workflow and immutable evidence ingestion in `apps/coire-api/src/coire_scheduler/training_guard.py`, `training_admission.py` and `coire_api/routes/admin_training.py`; freeze workload/query identity, require >=100 completions per target per 15-minute phase and mark under-sampled results inconclusive.
- [X] T100 [US4] Enforce reverse mixed-workload checks for existing single/sharded loads, image dispatch, conversions and gateway leases in `apps/coire-api/src/coire_scheduler/placement.py`, `sharded_instances.py`, `image_dispatch.py`, `acquisition.py` and `coire_api/gateway/proxy.py`; no unmanaged path can bypass training ownership.
- [X] T101 [US4] Implement per-target trailing-5-minute chat p95/thermal/swap/footprint/progress monitoring and protective pause in `apps/coire-api/src/coire_scheduler/training_guard.py` with node `training/guard.py`; require >=30 first-token samples and <=60 s telemetry freshness for latency eligibility, block new mixed admission on insufficient data, and enforce <=5 s checks, <=60 s pause/forced-stop and guarded cooldown.
- [X] T102 [US4] Implement declared two-rank native launch/preparation and coordinated stop in `apps/coire-node/src/coire_node/training/supervisor.py`, reusing existing JACCL hostfile/launcher facilities without API-side SSH, raw caller hosts or control-fabric data fallback.
- [X] T103 [US4] Implement identical initial parameter verification, deterministic rank partitioning and common evaluated-update checkpoint coordination in `apps/coire-node/src/coire_node/training/worker.py`, `sampler.py` and `checkpoints.py`, preserving full per-rank RNG/optimizer state.
- [X] T104 [US4] Extend durable workflow/recovery to all-rank manifests, both complete bundle copies, link/rank loss and atomic new-attempt fencing in `apps/coire-api/src/coire_scheduler/training.py` and `training_recovery.py`; never resume with changed world size/runtime.
- [X] T105 [US4] Add measurement/profile CLI, admin console measurement submission/status and queue/capacity/protection diagnostics in `apps/coire-api/src/coire_api/cli.py`, `apps/coire-web/src/components/training/TrainingMeasurements.tsx`, `TrainingForm.tsx` and `TrainingRun.tsx`; make unmeasured and impossible-fit reasons distinct.
- [X] T106 [US4] Add simulated two-rank partial-start/partial-write/replication/grant-refresh/link-loss/stale-output tests in `tests/integration/test_training_distributed.py`, asserting complete common checkpoint choice and retained uncertain holds.
- [X] T107 [US4] Add profile measurement/invalidity and automatic-versus-admin-pause UI tests in `apps/coire-web/src/pages/Training.test.tsx`; regenerate affected OpenAPI/TS and rerun controller races.
- [X] T108 [US4] Add an authorized real-cluster acceptance driver in `scripts/validate-sft-training.py` using typed admin/gateway paths and protected credentials, with no direct trainer spawn or CI Studio execution; output only metadata/measurements to external artifacts.
- [X] T109 [US4] Execute real LoRA/QLoRA/DoRA train/checkpoint/resume/serve trials and three Studio-restart trials via `scripts/validate-sft-training.py`; record models/runtime/digests and measured numeric/control results in `specs/016-sft-training-jobs/execution-record.md`.
- [X] T110 [US4] Execute real two-rank completion plus rank-0/rank-1/data-link/transfer failure trials from `quickstart.md` section 7; record full per-node memory, common checkpoint/fence and no partial publication evidence in `execution-record.md`.
- [X] T111 [US4] Measure identical frozen-workload 15-minute baseline and mixed profiles with >=100 completions per resident target per phase, including pinned ops residency, positive training progress, <=1.5 s p95 first token and zero swap growth; record actual workload/query/report identities in `specs/016-sft-training-jobs/execution-record.md` and keep failed/inconclusive profiles disabled.
- [X] T112 [US4] Inject stale evidence, thermal/latency/memory guard conditions and test bidirectional image/train admission plus newer-pin-aware reload using `scripts/validate-sft-training.py`; record protective timings and recovered reservations in `execution-record.md`.
- [X] T113 [US4] Record the full single/two-node supported capability matrix and operational queue/eviction/profile restrictions in `docs/runbooks/sft-training.md` and `docs/ARCHITECTURE.md`; do not claim unsupported combinations.
- [X] T114 [US4] Execute the independent US4 acceptance matrix and verify all capacity/recovery findings are resolved in `specs/016-sft-training-jobs/execution-record.md` before declaring two-rank or mixed-workload capability complete.

## Phase 7 — Cross-cutting operations and release gates

- [X] T115 Add and test baseline training-stall/recovery/checkpoint/guard alerts in `deploy/observability/alerts/training.yaml` and `deploy/observability/tests/training.test.yaml`; verify labels, firing/clearing, runbook links and no sensitive metric cardinality.
- [X] T116 Add Training/Jobs panels for loss-history links, queues, attempt recovery, checkpoint replication, memory and chat protection in `deploy/observability/grafana/dashboards/jobs.json`, with explicit diagnostics-disabled behavior and persisted API history links.
- [X] T117 Complete private dataset volume mounts, scheduler/API-only access and frozen node packaging in `deploy/compose/compose.yaml`, `deploy/compose/README.md`, `apps/coire-node/install_runtime.py` and `scripts/stage-node-wheels.py`; preserve all existing image/network/hardening constraints.
- [X] T118 Complete kill/observe/recover/retain/backup/rollback operations in `docs/runbooks/sft-training.md` and reconcile `docs/ARCHITECTURE.md` §8.1 and `docs/design/DESIGN.md` status/feature-017 wording with implemented behavior.
- [X] T119 Run complete Python static/unit/contract/controller/tiny-engine and web test/lint/build gates from `specs/016-sft-training-jobs/quickstart.md`; record exact commands/counts and investigate every new failure or required skip in `execution-record.md`.
- [X] T120 Verify generated `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts` freshness and all existing chat/image/run/registry/failover regression suites, including <=20 ms p95 gateway overhead, in `execution-record.md`.
- [X] T121 Build affected production images/node wheelhouse through existing CI tooling and verify non-root/read-only/health/network policy, image scans and SBOM; record immutable build outputs/results in `specs/016-sft-training-jobs/execution-record.md`.
- [X] T122 Validate actual trace stage attribution with diagnostics on and durable job/audit/alerts with diagnostics off, including injected stall/protection faults; record evidence in `specs/016-sft-training-jobs/execution-record.md` and link the completed alert tests.
- [X] T123 Exercise disable/drain/fence/preserve/rollback plus safe migration downgrade refusal and disposable clean downgrade/upgrade using `docs/runbooks/sft-training.md`; record measured restore/compatibility outcomes in `execution-record.md`.
- [X] T124 Review every FR/SC against its mapped tasks below, update spec implementation status only after required evidence passes, and prepare a local `.github/PULL_REQUEST_TEMPLATE.md`-shaped summary in `specs/016-sft-training-jobs/handoff.md` citing Constitution I–VII, dependencies/licences and the spec; PR creation still requires user instruction.

## Dependencies and execution order

### Integration findings requiring follow-up (2026-10-06)

- [X] T125 Resolve the real measurement preparation-rejection/stop-proof gap in node `training/measurement.py`, `routes/training_measurements.py` and scheduler `training_measurements.py`: persist an immutable fenced never-started rejection so a refused prepare can produce an authenticated scope-matched stop proof without allocating a trainer or authorizing later start. Preserve unknown ownership and counted holds for missing/corrupt journals or lost acknowledgments; add real persisted-state/restart/replay tests before closing T075/T076/T099/T123. Reconcile live measurement `3f6f0a87-18eb-4988-83a9-ac19fb71fc71` through the authenticated protocol, never a manual database hold release.
- [X] T126 Diagnose and resolve complete accelerator-inventory eligibility for the installed unprivileged node service in `coire_node/agent.py`: current service-user process reads return hundreds of `AccessDenied` observations, so do not fabricate vacancy or silently ignore inaccessible candidate workloads. Obtain concrete native eligibility evidence and an architecture-consistent bounded solution before repeating T109/T111; any privilege-setting change requires explicit approval. Verified by the native inventory evidence above and authenticated successful measurement `2af7e71e-dd73-4e92-b50b-1f2e9bc0850a`; no privilege change.
- [X] T127 Verify live closure of the hardware-identity mismatch in `coire_node/agent.py`: use the installed package version advertised by registration/health rather than the shared control-plane SERVICE_VERSION setting, compare with core `training_guard.hardware_digest` in cross-component tests, then run a successful authenticated native memory measurement before closing the dependent training acceptance gates. Active candidate `0.2.0-b7ef530d9303` on both Studios and the successful 12-update A measurement prove live closure; broader training acceptance remains open.
- [X] T128 Activate and verify ordinary-attempt pristine preparation-rejection reconciliation using the existing staged `0.2.0-f8fa2f9ccede` candidate on both Studios. Preserve exact expired prepare rebinding and positive stop proof in `coire_api/training_executor.py` and node `training/rejections.py`/`supervisor.py`; reconcile cancelling job `01M493BHBQ1BBTDZHDF3TEGD5P`, attempt `01M493BRPDQB758P59E0VJYPCW`, through the authenticated scheduler lane before retrying queued measurement `271c04bb-1ad8-4670-93e3-c07d1de1d879`. Operator activated both candidates; authenticated health/reconciliation pass, job cancelled with both attempts' positive stop proofs and released memory. The queued measurement succeeds at 12 updates. No manual hold release.
- [X] T129 Resolve the demonstrated interpreter-binding defect before repeating physical checkpoint/adapter and two-rank acceptance. The old runtime's BSD socket fails unprivileged but succeeds under operator-root; the user confirmed all existing Python/Coire Local Network entries enabled. The existing dedicated `coire-node-python` succeeds unprivileged on both Studios. Reproduced uv 0.12.7's managed-install shortcut substituting shared `python3.13`; corrected explicit venv creation, executable-bound immutable identity and staging/reuse/activation guards. Red-to-green tests pass; frozen candidate `0.2.0-aec82cd3280b` on both Studios passes unprivileged BSD and actual `DataFabricClient` peer HTTP checks. Both service-preserving activation dry runs pass; exact noninteractive activation refuses missing sudo authentication. Activate that candidate on both, verify authenticated live data health and run a genuine scoped mirrored-checkpoint trial before closing this task. No permission, firewall, service-user or shared-interpreter change. Prior recipe trial `01M49AQ2639P8MJ7SKHKD8MNYF` is cancelled with all three attempts' positive stop proofs and released memory; partial checkpoints remain private/replicating.


- [X] T130 Resolve the physical two-rank progress collision in `coire_scheduler/training.py`: preserve both authenticated, immutable participant mailboxes while rank zero publishes the single job-level loss series. Add real-Postgres tests with different timing/footprint/timestamps in both arrival orders, preserve component barriers/replay immutability, and repeat genuine two-rank checkpoint/adapter inference. Regression first failed twice, then passes; live job `01M49XM6ZRRS7ARYYJPK9NQ33E` completes 12 updates with common update-4/8/12 mirrored checkpoints and served adapter.
- [X] T131 Configure tested digest-pinned agent/relay images on both Studio services with service settings preserved; verify authenticated health and matching Core run-broker pins, then perform genuine exact-target harness evaluation and Studio-owned MCP apply. Operator activation completed on both Studios, current health reports configured pins, all four verified 1.5B adapter harness evaluations passed, and MCP apply succeeded in a disposable workspace. Credentials remain in the explicit private acceptance Keychain; no image pull or network/privilege widening was used.

```text
Setup T001–T003
  -> shared schemas/persistence T004–T016
  -> safe runtime/data/checkpoint/admission foundation T017–T032
  -> US1 T033–T063 (single-node recipe/form/adapter loop)
  -> US2 T064–T080 (full recovery/controls)
  -> US3 T081–T094 (complete data/mixture lifecycle)
  -> US4 T095–T114 (two ranks and measured chat priority)
  -> release T115–T124
```

- All public story tasks depend on Phase 2. T032 is an early feasibility gate, not optional polish.
- US2 depends on the US1 job/worker/adapter loop. Its fake-node races are independently testable
  once that exists; hardware acceptance is explicitly completed again in US4 release trials.
- US3's analysis/mixture work can be developed after Phase 2, but writes to shared dataset/CLI/web
  files must be serialized with US1/US2. Its full mixture resume check requires US2.
- US4 depends on full checkpoint/recovery and deterministic sampler foundations, not merely a
  successful single-node training demo. T109–T112 require authorized real-Studio prerequisites.
- Final observability/packaging work can be prepared earlier, but final gates depend on all four
  stories. Add spans/metrics alongside each code path; T115/T116 integrate alerts/panels, not defer
  instrumentation until the end.
- Shared file edits (`db.py`, shared schemas, `cli.py`, `agent.py`, `App.tsx`, schema generation)
  are serialized. The one feature migration stays reversible; if delivery is explicitly split
  into PRs later, amend the migration/delivery plan before creating another revision.

## Parallel examples

- US1: T033/T034/T035 write independent test files after common fixtures; implementation follows
  once each failing assertion is understood. Do not run T042–T050 in parallel blindly: target
  resolution and shared lifecycle contracts must converge first.
- US2: T064/T065/T066 exercise separate node, transaction and engine boundaries after US1.
- US3: T081/T082 are independent split and sampler tests after shared contracts; subsequent
  dataset implementation must converge before UI/CLI consumes it.
- US4: T095/T096 cover independent transaction and evidence policies. Real Studio fault workloads
  T109–T112 are serialized so one acceptance run does not invalidate another's measured conditions.

## Requirement coverage

| Requirement | Primary tasks |
| --- | --- |
| FR-001 | T004, T007, T016, T038, T056 |
| FR-002 | T016, T033, T038, T056, T063 |
| FR-003 | T005, T006, T020, T027, T090 |
| FR-004 | T019, T020, T021, T044, T090 |
| FR-005 | T006, T013, T027, T081, T083 |
| FR-006 | T017, T019, T026–T028, T090 |
| FR-007 | T028, T085, T087–T089, T093 |
| FR-008 | T081–T084, T088, T091–T092 |
| FR-009 | T007, T018, T037 |
| FR-010 | T017–T018, T037, T053, T060, T109 |
| FR-011 | T029–T030, T095, T097–T100, T111 |
| FR-012 | T030, T038–T039, T097, T105 |
| FR-013 | T065, T071, T097, T112 |
| FR-014 | T022–T023, T037, T067, T103 |
| FR-015 | T032, T064–T070, T075–T076, T109 |
| FR-016 | T023–T025, T067, T077–T078, T104, T110 |
| FR-017 | T041, T072–T074, T078 |
| FR-018 | T015, T037–T038, T054–T055, T074 |
| FR-019 | T013, T035, T041, T051, T060 |
| FR-020 | T008, T034, T042–T048, T061 |
| FR-021 | T095, T097, T102–T104, T110 |
| FR-022 | T068–T069, T102–T104, T106, T110 |
| FR-023 | T053, T056, T059 |
| FR-024 | T014, T026, T033, T051, T076 |
| FR-025 | T005–T006, T019–T021, T026, T090 |
| FR-026 | T030, T096–T101, T107, T111–T112 |
| FR-027 | T017–T018, T026–T028, T088 |
| FR-028 | T013–T014, T035–T041, T064–T068, T106 |
| FR-029 | T036, T064, T069–T074, T080 |
| FR-030 | T013, T038, T067–T070, T077–T079, T086 |
| FR-031 | T017–T018, T028, T036–T037, T117 |
| FR-032 | T034, T041–T051, T057, T061 |
| FR-033 | T007, T037, T046, T057 |
| FR-034 | T054–T059, T062, T074, T088–T089, T105–T107 |
| FR-035 | T011, T016, T027–T030, T077–T079, T093–T094, T117–T118 |
| FR-036 | T015, T037, T062, T098–T101, T115–T116, T122 |

| Outcome | Acceptance tasks |
| --- | --- |
| SC-001 | T059–T063, T109 |
| SC-002 | T022–T023, T066, T075, T091, T109 |
| SC-003 | T034, T042–T050, T057, T061, T063 |
| SC-004 | T015, T054, T059, T074, T122 |
| SC-005 | T019–T020, T090 |
| SC-006 | T095–T101, T110–T112 |
| SC-007 | T026–T028, T081–T084, T089–T092 |
| SC-008 | T034, T046–T048, T061 |
| SC-009 | T064–T076, T109–T112 |
| SC-010 | T102–T104, T106, T110 |
| SC-011 | T024, T033–T035, T064–T068, T076, T106 |
| SC-012 | T115–T116, T122 |

## Incremental implementation strategy

The first demonstrable slice is Phase 2 + US1: a small single-node recipe/form job with a real
private served adapter. It is an implementation checkpoint, **not feature-016 completion**.
Full-state recovery, all dataset guarantees, data parallelism and measured chat priority remain
required. Keep default-off exposure and capability-specific refusal until each path is verified.
Finish with the full quickstart, images/scans/rollback, and evidence-backed requirement review.

## Planning handoff

The user invoked `/speckit.implement`; the continuous controller is bound to this feature.
Only verified tasks are checked. See `execution-record.md` for results and prerequisites.


- [X] T132 Close the live legacy `/v1/completions` 404 from quickstart §6 using
  typed single-string prompt compatibility through the existing authenticated
  exact-target chat path; preserve SSE usage/cancellation, test registry refusal
  and fragmented stream conversion, regenerate contracts and record live trials.
- [X] T133 Close the live checkpoint-promotion 503 caused by the unset temporary
  extraction app-state flag; retain disabled-admission refusal, run the route
  regression and prove the promoted adapter reaches ready through native extraction.

- [X] T134 Close the confirmed native worker telemetry initialization gap: Tempo
  contains parent start/ingest spans but no worker execute span, and subprocesses
  neither receive OTLP_ENDPOINT nor configure providers. Pass only the configured
  nonsecret endpoint, initialize native numerical and CPU analysis entry points, prove real SDK span and
  metric export, stage/test frozen node builds, activate after existing measurements
  stop, and repeat current-runtime profile/diagnostic acceptance. Preserve bare
  engine ownership, immutable runtime evidence and credential-free worker environments.

- [x] T135 Close live false checkpoint-overdue alarms from cancelled/fenced attempts only after every rank has immutable death proof; keep current checkpoint and unresolved old ownership alarms, prove the SQL cases in disposable Postgres and actual alert clearing after deployment.

- [X] T136 Correct live single-node coexistence resident-health validation: verify the exact READY owned engine instead of the sharded-only rank flag; prove ordinary single-node acceptance and changed/stopped engine rejection in Postgres, deploy gated images and complete the real frozen workload.

- [X] T137 Keep native measurement journal reads, observations and lease-status responses off the request event loop; prove an occupied journal leaves requests responsive, preserve deadline/death-proof checks, package and gate the runtime, and activate after the current live experiment finishes.

- [X] T138 Diagnose strict gateway measurement failures with closed reason labels in structured logs, spans and counters; distinguish occupied concurrency, completion timeout, identity mismatch and operation failure without prompt or exception contents. Preserve fixed arrivals, complete windows and every existing acceptance gate; cover occupied-slot and timeout cases before repeating the real workload.

- [X] T139 Correct real training idle eviction: exclude training-only drain decisions from ordinary model-placement dispatch, and avoid probing absent native status before the durable drain barrier permits preparation. Prove both behaviors with Postgres regressions; preserve counted holds and cancellation proof, then repeat actual image and newer-pin acceptance.

- [X] T140 Investigate actual fixed-arrival gateway failures: Tempo records httpx.RemoteProtocolError before response headers. Bound idle upstream connection reuse conservatively, prove expired connections are replaced with a local TCP peer regression, and repeat the identical full Studio workload. Never retry an already-started stream or remove failed requests from evidence; treat the idle-close race as a hypothesis until live comparison passes.

- [X] T141 Keep ordinary watchdog journal reads, engine proxy ownership snapshots and periodic engine reconciliation inventory off the request loop when the shared native admission lock is occupied; prove heartbeat responsiveness before/after, preserve exact engine/adapter validation and durable ownership, and add node proxy lookup/adapter/upstream spans to distinguish lock delays from model work. Gate a frozen runtime and repeat sustained acceptance before attributing the live cause.

- [X] T142 Close independently observed Tempo OOM restarts during diagnostic acceptance: keep a fixed container memory ceiling, set a Go heap budget below it, preserve existing traces and hardening, and prove sustained ingestion plus exact trace reads without further restarts.

- [X] T143 Close sampled gRPC fork-handler stalls caused by background native metrics probes: use macOS posix_spawn with close-on-exec-by-default, explicit stdio, credential-free environment and bounded capture/deadlines; prove no fork and no inherited descriptors, gate a frozen runtime, and repeat unchanged sustained coexistence.
