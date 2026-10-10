# Implementation Plan: Preference Optimisation and Feedback Capture

**Branch**: `feat/018-preference-optimisation` | **Date**: 2026-10-08 | **Spec**: [spec.md](spec.md)
**Baseline**: `d2e4bbf` (merged training 016, evaluation 017 and node budget PR 94).
**Status**: Application/native implementation and exclusive/privacy/recovery/rollback gates pass. Three exact shared profiles pass; dense ORPO shared qualification and required hosted resource CI remain pending (T074/T076/T077). See [execution-record.md](execution-record.md).

## Summary

Close the explicit feedback → private preference dataset → adapter-training loop. Add owner thumbs, isolated response comparisons, a capture setting with withdrawal cleanup, admin review and durable export. Extend the existing bare MLX training lifecycle with DPO/ORPO through version 3 intent, exact initialization, objective-specific measurements, full recovery and feature 017 evaluations. Preserve v1/v2 SFT documents and existing verification gates.

## Technical Context

**Language/Version**: Python 3.13; strict TypeScript and React 18.
**Primary Dependencies**: Existing Pydantic v2, FastAPI, SQLAlchemy 2/asyncpg, Alembic, DBOS in scheduler only, httpx, OTel and React/Vite. Native lock remains MLX 0.32.2 / mlx-lm 0.31.3. The API adds exactly pinned uvloop 0.22.1 (MIT / Apache-2.0) for Uvicorn I/O scheduling; native dependencies remain unchanged. ADR-0014 records the measured gateway work. Use upstream `train`/`evaluate` loss and iterator hooks; do not fork the trainer or install a separate framework.
**Storage**: Postgres 17 for capture generations, feedback/pairs, provenance, export intent, lineage and durable metrics; existing private training-data volume for immutable dataset bytes and bounded staging; node-owned mirrored adapter/checkpoint stores. One reversible migration after 0032.
**Testing**: Unit/contract/Postgres race/restart tests, Vitest/browser accessibility, CPU-independent objective reference math, isolated Apple Silicon tiny-model engine tests, real-Studio training/recovery/serving/coexistence acceptance, Ruff/mypy/OpenAPI/TS, container build/scan/SBOM, Prometheus rule tests.
**Target Platform**: Existing core services and two 256 GB Studios; no new service, container, network or engine port.
**Project Type**: Existing monorepo API/CLI/SPA and native node agent.
**Performance Goals**: 500 ms p95 feedback/settings mutation, 1 s review page, five-minute 10,000-pair export publication deadline excluding node analysis; five-second reachable training cancellation; existing 20 ms gateway / 1.5 s loaded chat p95 limits; zero swap growth.
**Constraints**: Native numerical work only on Studios; authenticated owner/admin boundaries; no model pull or executable dataset/objective plugins; preserve audit and alerts when diagnostics are off; known-liveness reservations; version-specific serialized identity.
**Scale/Scope**: ≤10,000 pairs / ≤256 MiB per export; one active export globally, ≤100 queued; one pending pair/conversation; ≤100 entries/page; candidate expiry 24 hours; initial DPO/ORPO × dense LoRA/affine QLoRA on one Studio. Preference DoRA/distributed/full tuning excluded.

## Constitution Check

Design gates passed before research and after design. These are not implementation or acceptance results.

| Principle | Before research | After design | Required evidence |
|---|---|---|---|
| I — Bare engines | PASS | PASS | Reuse unchanged bare mlx-lm trainer with small allowlisted loss/iterator adapters; only coire-node owns processes. No wrapper or engine port change. |
| II — Core/Studio placement | PASS | PASS | Core handles bounded text validation/storage; node performs tokenization/model loading/loss/evaluation. Tests reject native execution on core. |
| II-a — Container hardening | PASS | PASS | Existing per-service hardened images only, no service addition. Existing build/scan/SBOM gates remain required. |
| III — Contracts first | PASS | PASS | coire-core shapes precede code; v3 union at every embedding boundary; frozen v1/v2 fixtures; generated OpenAPI/TS. No `/v1` response changes. |
| IV — Trust | PASS | PASS | Ownership, admin scope, bounded grants, withdrawal generation locks, request idempotency and content-free audit. No new egress or broad permission. |
| V — Registry/verification | PASS | PASS | Exact acquired local base/adapter inputs, pinned manifests, new result initially private/unverified, no inherited verification. |
| VI — Observability | PASS | PASS | Durable objective/progress history, local traces/logs, bounded metrics, dashboard and baseline alerts; prompt/answer text excluded. |
| VII — Spec/test gates | PASS | PASS | Design precedes implementation; per-interface contracts, numerical and tiny-model tests; real cluster acceptance required before release. |

No constitution exception. Document the objective-hook choice and support matrix in ADR-0014 during implementation; this records the roadmap's alternative to adding `mlx-lm-lora` without expanding the permitted engine boundary.

## Project Structure

```text
specs/018-preference-optimisation/
  spec.md, plan.md, research.md, data-model.md, quickstart.md, tasks.md
  contracts/feedback-api.md, contracts/preference-training.md
  checklists/requirements.md, handoff.md
packages/coire-core/src/coire_core/
  models/feedback.py, models/preference.py                  # new contracts
  models/training.py, models/training_node.py, models/adapters.py
  models/chat.py, models/datasets.py, models/node.py
  training_sampling.py
  preference_data.py, training_data.py, settings.py, errors.py
apps/coire-api/src/coire_api/
  feedback/{service,comparisons,eligibility,exports,retention,telemetry}.py
  routes/chat_feedback.py, routes/admin_feedback.py
  chat/{turns,service,processing,streaming,maintenance}.py
  gateway/{resolution,targets,execution}.py, db.py, app.py, cli.py
  training/{specs,datasets,adapters,measurements,checkpoints,input_grants,retention}.py
  evaluation/{training,authorization,inputs}.py
apps/coire-api/src/coire_scheduler/
  feedback.py, main.py, workers.py
  training.py, training_{controller,admission,measurements}.py
apps/coire-api/alembic/versions/0033_preference_feedback.py
apps/coire-node/src/coire_node/
  training/preference_{loss,runtime,data}.py                 # new numerical adapters
  training/{worker,objectives,sampler,rendering,checkpoints,measurement,analysis_worker}.py
  training/{supervisor,retention,extraction}.py
  routes/training.py, routes/training_measurements.py, agent.py
apps/coire-web/src/
  api/feedback.ts, api/schema.d.ts
  components/chat/{FeedbackControls,Comparison,FeedbackSettings}.tsx
  components/feedback/{ReviewQueue,ExportForm,ExportHistory}.tsx
  components/training/{TrainingForm,TrainingActivity,AdapterPanel,DatasetPanel}.tsx
  pages/Chat.tsx, pages/Training.tsx
recipes/training/{dpo,orpo}.yaml
apps/*/tests/, packages/coire-core/tests/, tests/integration/
deploy/observability/{alerts,tests,grafana/dashboards}/
docs/runbooks/preference-training.md, docs/runbooks/feedback.md
```

New paths are proposed implementation files; existing files stay in their shipped package. The scheduler source is under `apps/coire-api/src/coire_scheduler`. New feedback modules extend existing route registration; no duplicate service is introduced.

## Phase 0 — Research decisions

[research.md](research.md) records source evidence and alternatives. Key findings: current chat retry cannot regenerate successful turns safely; historical turns lack a complete exact execution snapshot; current training measurement is v1-only; evaluation relies on explicit v2 type checks; immutable v1/v2 hashes preclude widening their objective fields. Upstream trainer hooks are sufficient for pair losses but its count/metric semantics require deliberate adaptation. The user confirmed the historical-snapshot withdrawal policy during this run.

## Phase 1 — Design

### Shared contracts and migration

Add strict feedback/pair/export and preference-data value types first, then new v3 training/resolved/checkpoint variants. Do not add serialized defaults to old immutable documents. Expand adapter objective values additively and expose lineage through a separate typed read projection. One migration creates capture preferences, pair/judgement/export/provenance state, active-answer/provenance links and additive v3 storage; no historical recipe rewrite. Downgrade requires drained v3 work and removal of new-only data through documented cleanup before removing tables/columns.

### Eligibility, chat and withdrawal

All owner paths reuse human chat authentication; service/run credentials cannot contribute or change a person's setting. Admin review never bypasses the owner's live eligibility. New eligible opted-in text turns persist the exact model-visible input, rendering settings and resolved `InferenceTarget` at execution, bounded and associated with the capture generation; this is private comparison provenance, not a training dataset. Turns missing this provenance are comparison-ineligible, with a user-visible explanation. Opt-out stops this capture and purges these additional copies, but leaves the ordinary chosen chat transcript intact.

Comparison is a new explicit operation, not `retry_of`. Reserve the conversation revision and pending pair; execute through the shared native chat/gateway path with frozen prompt/target, ordinary quota and generation ownership. Persist the candidate separately until completion. An explicit active-answer mapping determines display and future model context. Selection of either candidate unlocks chat; the owner may revise a judgement later but cannot rewrite conversation context after new turns exist. Dismissal/expiry/failure retains the original. Admin review never changes conversation context. Existing SSE transport carries typed comparison lifecycle events and reset snapshots; reattach never regenerates.

Owner setting generation, conversation tombstone and judgement versions form the eligibility fence. Use one lock order: capture-preference owner IDs sorted, conversation IDs sorted, then pair/feedback IDs sorted. Capture/selection/review, deletion and export publication follow that order. Disable/delete immediately invalidates readers and writers and schedules bounded idempotent purge within 24 hours; restoring capture increments generation again and never revives old contributions. Chosen ordinary chat messages follow chat retention; copied prompt/alternative content follows feedback withdrawal. Content-free audit/tombstones survive. Published dataset snapshots remain unchanged under the disclosed policy.

### Durable export and preference data

Scheduler DBOS work carries IDs only. Snapshot bounded candidate IDs, selected source/version and filters; reserve private bytes; stream canonical rows plus provenance into contained staging without holding user locks. At publication, lock live eligibility and source versions. Changed contributions require a fresh bounded rebuild outside locks; do not publish stale staged content. Publication means the atomic committed dataset registration and immutable source/split/provenance identity, usually with dataset state `analyzing`; it does not mean token analysis has finished. Crash reconciliation distinguishes unpublished orphan bytes from committed datasets, and keeps disk accounting until physical cleanup is confirmed.

Only complete explicit pairs export. Thumbs never synthesize preference labels. `owner_preferred` produces one row per pair with owner choice taking precedence; owner/admin-only modes remain explicit. Text schema uses a shared prompt and distinct chosen/rejected assistant text, with canonical prompt-group splits to prevent reversed labels or alternate completions leaking into validation. Core validates/hashes/splits; Studio analyzes both completions with the exact tokenizer/template and rejects zero-token/overlength samples without truncation. Prefer sequential bounded scans over an in-memory corpus. Export warning under 20 pairs; readiness failure under two distinct prompt groups.

### V3 training, math, initialization and recovery

Use the exact contract in [preference-training.md](contracts/preference-training.md). V3 contains `objective`, `init_adapter`, bounded objective options and optional feature 017 suites. The single-node policy uses the shipped dense/QLoRA allowlist. DPO loads an independent frozen initial-policy reference (including adapter); ORPO has no reference. Both use unchanged upstream train/evaluate with custom batches/loss. Count pairs for pair-averaged loss aggregation; report token throughput separately. Compute bounded accuracy/margin probes outside compiled gradient loss with RNG/sampler preservation.

Validate all local manifests and pin initial/base artifacts before launch. Fresh training loads initial adapter tensors after architecture injection; resume loads full policy+optimizer+RNG+sampler checkpoint state. DPO always reconstructs reference from immutable initial artifacts, never from resumed policy. Checkpoint schema/identity includes objective implementation, settings, initial/reference, pair sampler, mask and template versions; mirrored full-state commit remains mandatory. Retention pins protect inputs for paused/recovering jobs. Output extraction writes a complete independent adapter, preserving ancestry as metadata. Existing exact serving and harness verification paths handle the new result.

Extend measurement request, configuration digest, node capability advertisement, native worker dispatch, lease/import/checkpoint/extraction paths and scheduler profile matching explicitly. V3 objective/settings/reference identity belongs in its profile key; an SFT or different objective profile cannot qualify it. Feature 017 helpers must select evaluated v2 or v3 documents without losing empty-suite v3 support; declared final/checkpoint obligations retain their existing semantics. DPO/ORPO scores remain informational.

### Rollout, limits and operations

`COIRE_PREFERENCE_TRAINING_ENABLED=false` independently gates new preference admissions (also require existing training enabled). Capture defaults enabled per owner with visible versioned disclosure, but controls are exposed only after the whole privacy-safe feedback foundation is installed. Bounds in contracts are constants except `COIRE_FEEDBACK_STORAGE_QUOTA_BYTES=1073741824` and `COIRE_FEEDBACK_PURGE_BATCH_SIZE=100`; settings live only in coire-core and compose README. Feedback quota counts copied database bodies and temporary files; temporary files share the existing volume with separate counted quota; exports additionally reserve existing dataset quota. One active export, 100 pending; five-minute execution deadline after admission, one-hour queue deadline and at most three publication rebuilds; unresolved cleanup remains visible and counted.

Disabling new preference training does not stop recovery, cancellation, export cleanup or privacy cleanup. Before binary rollback, disable submissions; stop and drain comparison generation, exports, v3 jobs and evaluation-owned pauses; retain published datasets/checkpoints for upgraded readers. Old binaries must never receive v3 payloads or resume them. Runbook distinguishes binary rollback from destructive schema downgrade and documents content handling, cancellation and audit lookup.

### Observability and validation

Spans: `coire.api.feedback.mutate`, `coire.api.feedback.compare`, `coire.scheduler.feedback.export`, `coire.scheduler.feedback.purge`, `coire.node.training.preference`. Metrics: `coire_feedback_mutations_total{kind,outcome}`, `coire_feedback_export_total{outcome}`, `coire_feedback_export_oldest_seconds`, `coire_feedback_purge_oldest_seconds`, `coire_feedback_storage_bytes`, and existing training metrics with bounded objective where appropriate. Objective loss/accuracy/margin series belong to authenticated durable job history, not model/user-labelled Prometheus series. Logs include applicable run/job/instance/model/user IDs; never prompts, answers, tags or dataset rows. Jobs panels link to export/lineage/history. Baseline alerts cover stuck export, overdue withdrawal purge and existing training memory/cleanup failures.

Meaningful acceptance includes auth/refusal audit, exact provenance and active-answer context, setting/delete races at every export boundary, content erasure from SSE caches/DBOS inputs/staging, source priority, grouped splits and overlap, frozen v1/v2 fixtures, DPO/ORPO math and gradient tests, immutable frozen reference, initial adapter/resume order, objective-aware resource measurements, full restart/kill, exact serving/verification, evaluation obligations, diagnostics-off alerts, quotas and drained rollback. [quickstart.md](quickstart.md) defines local/CI and real-Studio gates. Planning itself supplied no engine evidence. Subsequent authorized implementation measurements and release status are recorded in [execution-record.md](execution-record.md).

## Phase 2 — Task generation

[tasks.md](tasks.md) orders shared privacy/contracts/storage foundations first, then US1 capture/export, US2 training, US3 setting UX/erasure proof and US4 admin review, followed by complete release qualification. Core privacy enforcement is foundational even though its user-facing story is US3. US2 may be independently tested using uploaded preference fixtures after foundations; it need not wait for organic feedback. Implementation was subsequently authorized and executed against these tasks; sustained shared-profile qualification remains pending.

## Complexity Tracking

No constitutional violation. New durable entities and v3 variants are necessary to keep withdrawal races, chat context and historical training identities correct. Reuse existing service/process, token, ledger, storage and recovery boundaries; no generic feedback platform, plugin system, new trainer framework or distributed preference engine is introduced.
