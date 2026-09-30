# Tasks: Image Generation

**Input**: `specs/015-image-generation/{spec.md,plan.md,research.md,data-model.md,contracts/images.md,quickstart.md}`.
**Tests**: Required by FR-035 and Constitution VII; write meaningful failing tests before the
corresponding behavior, then keep each committed increment green.
**Organization**: Foundations -> US4 authorization (P1) -> US1 generation (P1) -> US2 reproduction
(P1) -> US3 caches (P2) -> US5 coexistence (P2) -> release verification. Stories retain spec IDs.

## Phase 1: Setup and reviewable delivery

- [X] T001 Record the original child delivery slices and parent task mappings in `specs/015-image-generation/delivery.md`; subsequent work follows the user's 2026-09-30 grouped local review direction recorded there (FR-035).
- [X] T002 Add native image import/install regression cases to `tests/unit/test_node_install.py`, covering frozen mflux/MLX dependency staging, preservation of text/VLM smoke, and no image runtime in core images (FR-004, FR-036).
- [X] T003 Pin Darwin-only mflux 0.20.0 in `apps/coire-node/pyproject.toml` and `uv.lock`; extend `apps/coire-node/install_runtime.py` and `scripts/stage-node-wheels.py` smoke/provenance as needed; record MIT reason and separately reviewed model licences in the child PR (FR-004, FR-015).

## Phase 2: Foundational contracts and persistence

**Goal**: Shared types, durable state and telemetry seams required by every story. Keep image
admission disabled until US4 and the applicable US1 safety/publication paths are complete.

- [X] T004 [P] Add strict request/recipe/capability/state validation tests to `packages/coire-core/tests/test_images.py`, including purpose-specific 10 MiB generation/64 MiB recipe bounds, modes, full-precision scales, PNG-only output, unsafe paths, unknown fields, request-intent idempotency and seed expansion (FR-001, FR-002, FR-028, FR-030).
- [X] T005 [P] Add registry-default, auxiliary-kind exclusion and node-message schema tests to `packages/coire-core/tests/test_image_worker.py`; cover UUID foreign keys, ULID jobs, attempt/fence mismatch and image backend discrimination (FR-015, FR-024, FR-036).
- [X] T006 Define versioned native/compatible/spec/recipe/job/event/input/output/preset/grant contracts in `packages/coire-core/src/coire_core/models/images.py`, including root-level optional submit overrides and immutable resolved types (FR-001, FR-002, FR-005, FR-007, FR-012, FR-013, FR-016, FR-025, FR-028, FR-030).
- [X] T007 Define strict node/worker/input-transfer commands in `packages/coire-core/src/coire_core/models/image_worker.py`; extend `models/registry.py`, `models/engine.py`, `models/acquisition.py`, `models/files.py` and `models/console.py` compatibly for image kinds/capabilities/status and isolated parser operations (FR-004, FR-015, FR-024, FR-030, FR-036).
- [ ] T008 Add Postgres migration/constraint/quota/cancel-publication-race tests to `apps/coire-api/tests/unit/test_image_persistence.py`, including existing text/VLM rows and downgrade-drain preconditions (FR-025, FR-026, FR-029, FR-031, SC-012).
- [X] T009 Add typed image persistence/indices to `apps/coire-api/src/coire_api/db.py` and reversible migration(s) under `apps/coire-api/alembic/versions/`, one per child PR after the actual current head; include jobs/events, preset revisions, inputs/outputs, grants/receipts, quotas and execution profiles/leases (FR-003, FR-005, FR-025, FR-026, FR-029, FR-031).
- [X] T010 Add canonical recipe/hash/seed helpers in `packages/coire-core/src/coire_core/models/images.py` and their tamper/precision tests in `packages/coire-core/tests/test_images.py`; exclude paths, identity and secrets and distinguish pixel digest from PNG bytes (FR-013, FR-014, FR-028).
- [X] T011 Add image settings, conservative bounds, feature enablement and safe `CoireError` subclasses in `packages/coire-core/src/coire_core/settings.py` and `packages/coire-core/src/coire_core/errors.py`; document env names/defaults in `deploy/compose/README.md` (FR-001, FR-023, FR-031, FR-036).
- [ ] T012 Add `coire-blobs`, least-privilege volume namespaces, image settings and route-specific purpose-specific bounded upload/proxy settings (64 MiB recipe file plus bounded multipart framing, application-enforced 10 MiB generation inputs) in `deploy/compose/compose.yaml`, `compose.override.it.yaml` and `apps/coire-web/nginx/nginx.conf`; no network/CORS/capability widening (FR-011, FR-023, FR-030, FR-031, FR-036).
- [X] T013 Add content-free span/log/metric helpers in `apps/coire-api/src/coire_api/images/telemetry.py` and node image telemetry in `apps/coire-node/src/coire_node/metrics.py`; test label cardinality and redaction in `apps/coire-api/tests/unit/test_image_telemetry.py` (FR-033).

## Phase 3: US4 — Explicit content authorization (P1)

**Independent test**: With a fake executor, an entitled human/personal key succeeds and audits;
a non-entitled caller, service/run credential, forged preset or revoked key fails before execution.
Classification tags never grant permission or filter an entitled prompt.

- [ ] T014 [P] [US4] Add route/actor/scope/origin/refusal-audit tests in `apps/coire-api/tests/contract/test_image_authorization.py`, covering ordinary/admin humans, personal keys, service/ops/run/legacy credentials and live revocation at all stages (FR-017, FR-018, FR-024, FR-027, SC-004, SC-005, SC-009).
- [X] T015 [P] [US4] Add preset revision/override/dependency-union tests in `apps/coire-api/tests/contract/test_image_presets.py`, including hidden dependencies, stale edits, retired models and malicious imported privilege fields (FR-003, FR-019, FR-027, FR-036).
- [X] T016 [P] [US4] Add offline classifier/tagging/timeout/memory tests in `apps/coire-node/tests/unit/test_image_classification.py`, including unchanged entitled prompts and unknown-tag behavior (FR-020, FR-021, FR-032).
- [ ] T017 [US4] Implement the image principal/owner/origin guard and effective entitlement resolver in `apps/coire-api/src/coire_api/images/authorization.py` and `auth.py`; reject non-human credentials, enforce personal scopes and recheck live user/key/dependency state (FR-017, FR-024, FR-027).
- [ ] T018 [US4] Implement explicit admission/refusal/completion audit and revocation-driven cancellation in `apps/coire-api/src/coire_api/images/service.py`, integrating `identity/entitlements.py` without changing its grant authority (FR-018, FR-027, FR-036, SC-009).
- [X] T019 [US4] Implement immutable preset resolution/precedence/once-only prefix and filtered listings in `apps/coire-api/src/coire_api/images/presets.py`; registry dependency requirements cannot be removed by overrides (FR-003, FR-019, FR-027).
- [X] T020 [US4] Add audited human-admin preset create/update/retire routes in `apps/coire-api/src/coire_api/routes/admin_images.py` and admin-imported templates under `recipes/images/`, with optimistic revisions and no acquisition side effects (FR-003, FR-036).
- [ ] T021 [US4] Implement direct Studio-CPU local-only safetensors tagging in `apps/coire-node/src/coire_node/image_runtime/classification.py`, with the pinned admin asset, measured reservation,10s deadline, recorded threshold/version, explicit-policy override and unknown fallback (FR-020, FR-021, FR-032).
- [ ] T022 [US4] Add reusable output-access/tag filtering and explicit download rechecks in `apps/coire-api/src/coire_api/images/authorization.py`; verify runtime audits, prompt preservation, unknown exclusion and no ordinary admin bypass with `test_image_authorization.py` (FR-018, FR-019, FR-020, FR-021, FR-024, FR-032, SC-004, SC-005, SC-009).

## Phase 4: US1 — Private generation, progress and cancel (P1)

**Independent test**: Submit from the UI and compatible API, receive a durable receipt or standard
result, observe ordered progress, download the complete private batch, and cancel without exposing
partial output. A local simulated worker proves recovery/ownership independently of hardware.

- [ ] T023 [P] [US1] Add native/job/gallery/grant/compatible/admin route contracts in `apps/coire-api/tests/contract/test_images.py` and `test_v1_images.py`, including timeout job recovery, key replay, expired grants and typed errors (FR-005, FR-007, FR-012, FR-016, FR-024, FR-025, SC-001, SC-010).
- [ ] T024 [P] [US1] Add worker lifecycle/node route/journal/idempotency/progress/cancel tests in `apps/coire-node/tests/contract/test_node_images.py` and `tests/unit/test_image_jobs.py`, including PID/create_time mismatch and no HF credential (FR-004, FR-007, FR-008, FR-025, FR-026, FR-029).
- [ ] T025 [P] [US1] Add deterministic queue/restart/fenced publication tests in `tests/integration/test_image_jobs.py` and `test_image_recovery.py` using a new fake worker in `apps/coire-node/src/coire_node/testing/fake_image_worker.py` (FR-005, FR-007, FR-025, FR-026, SC-010, SC-011, SC-012).
- [ ] T026 [P] [US1] Add storage/quota/grant/receipt/cleanup tests in `apps/coire-api/tests/unit/test_image_storage.py` and web receipt/progress/auth-error tests in `apps/coire-web/src/pages/Images.test.tsx` (FR-008, FR-011, FR-012, FR-023, FR-029, FR-031, FR-034, SC-007, SC-008).
- [ ] T027 [US1] Add kind-aware image/component inspection and licence/manifest validation in `apps/coire-api/src/coire_api/registry/inspection.py` and `apps/coire-node/src/coire_node/hub.py`; tests in `apps/coire-api/tests/contract/test_image_acquisition.py` must precede changes and reject remote-code/pickle/default downloads (FR-015, FR-036).
- [X] T028 [US1] Exclude image and auxiliary assets from chat/VLM/MCP/failover resolution in `apps/coire-api/src/coire_api/registry/service.py`, `gateway/resolution.py`, `routes/models.py` and snapshot assembly; add routing regression tests in `apps/coire-api/tests/contract/test_image_isolation.py` (FR-015, FR-024, FR-036).
- [ ] T029 [US1] Implement resident fixed-pipeline txt2img execution and synchronized callbacks in `apps/coire-node/src/coire_node/image_worker.py` and `image_runtime/pipeline.py`, with strict local manifest preflight and no prompt/model content filter (FR-001, FR-002, FR-004, FR-007, FR-020).
- [ ] T030 [US1] Extend image process launch/readiness/status/re-adoption in `apps/coire-node/src/coire_node/engines.py`, `store.py` and `agent.py`; persist pid/create_time/port/reservation before return, isolate authenticated loopback worker control and keep default one executor/node (FR-004, FR-005, FR-025, FR-036).
- [X] T031 [US1] Write canonical recipes in uncompressed PNG iTXt `coire.image` chunks and deterministic output/pixel manifests in `apps/coire-node/src/coire_node/image_runtime/metadata.py`, bounded to 64 KiB and preserving every effective numeric value (FR-013, FR-028).
- [ ] T032 [US1] Implement node image input staging/job journal/start/status/cancel/cleanup commands in `apps/coire-node/src/coire_node/image_jobs.py` and `routes/images.py`; fence duplicate/mismatched attempts and enforce node-local deadlines (FR-007, FR-008, FR-025, FR-026, FR-029).
- [ ] T033 [US1] Implement reserved txt2img/auxiliary validation and thumbnail evidence in `apps/coire-node/src/coire_node/image_validation.py` and existing acquisition executor; require both-copy hashes and complete offline local trees before publishing (FR-015, FR-030, FR-036).
- [ ] T034 [US1] Implement deterministic image DBOS dispatch/recovery in `apps/coire-api/src/coire_scheduler/images.py`, `main.py` and `workers.py`; observe existing node journal/receipts after restart, refuse unvalidated chat overlap and never blindly regenerate (FR-005, FR-025, FR-026).
- [ ] T035 [US1] Implement admission, client-intent key replay, queue limits, daily image allowance and worst-case disk holds in `apps/coire-api/src/coire_api/images/service.py`; receipt follows job/quota/audit commit and repeated submission charges once (FR-001, FR-005, FR-023, FR-025, FR-027, FR-031, SC-010, SC-012).
- [ ] T036 [US1] Implement scoped transfer grant minting/receipt reconciliation in `apps/coire-api/src/coire_api/image_executor.py` and `images/storage.py`, with scheduler DB minting, node-bound hashes/deadlines and streamed size/digest validation (FR-008, FR-011, FR-024, FR-029).
- [ ] T037 [US1] Add native eligible model/preset lists, submit/list/get/cancel and node transfer transport to `apps/coire-api/src/coire_api/routes/images.py`, `app.py` and `nodes_client.py`; enforce current authorization and safe RFC9457 failures (FR-005, FR-007, FR-024, FR-026).
- [ ] T038 [US1] Implement whole-batch publication, private gallery projections, owner grants/download/delete in `apps/coire-api/src/coire_api/images/storage.py` and `routes/images.py`; publication requires complete durable receipts, live access and node cleanup acknowledgment (FR-008, FR-011, FR-012, FR-013, FR-021, FR-024, FR-029, FR-031).
- [ ] T039 [US1] Implement node scratch cleanup/ack recovery and core tombstone/orphan/quota sweeps in `apps/coire-node/src/coire_node/image_jobs.py` and `apps/coire-api/src/coire_api/images/maintenance.py`; do not declare success with retained Studio bytes (FR-008, FR-011, FR-029, FR-031, SC-007, SC-008).
- [ ] T040 [US1] Implement persisted replay/reset/heartbeat/terminal events in `apps/coire-api/src/coire_api/images/events.py` and `routes/images.py`, with bounded 4 Hz progress, current auth and disconnect-independent execution (FR-007, FR-024, FR-026, SC-010).
- [ ] T041 [US1] Complete cancel/publication arbitration, scheduler kill dispatch and node TERM/KILL escalation in `apps/coire-api/src/coire_api/images/service.py`, `coire_scheduler/images.py` and `coire_node/image_jobs.py`; retain uncertain reservations and prove <=5s healthy stop (FR-007, FR-008, FR-026, SC-008, SC-011, SC-012).
- [ ] T042 [US1] Implement the bounded synchronous compatible adapter in `apps/coire-api/src/coire_api/routes/v1_images.py`; map only supported fields into native admission, standard url/base64 results,90 s HTTP 504 with `coire_job_id`, and same-key recovery (FR-016, FR-024, FR-025, SC-001).
- [ ] T043 [US1] Regenerate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`; add typed transport in `apps/coire-web/src/api/images.ts` and extend shared event hook support without direct component fetch (FR-007, FR-016, FR-024, FR-026).
- [ ] T044 [US1] Implement Images routing/dock, preset rail and capability-driven basic form in `apps/coire-web/src/App.tsx`, `components/AppShell.tsx`, `pages/Images.tsx`, `components/images/ImageForm.tsx` and `PresetRail.tsx` (FR-001, FR-003, FR-019, FR-034).
- [ ] T045 [US1] Add role-gated preset save/edit/retire controls in `apps/coire-web/src/components/images/PresetEditor.tsx` with generated API types and optimistic-revision tests; preserve the source preset when editing and surface stale/dependency errors (FR-003, FR-019, FR-034, FR-036).
- [ ] T046 [US1] Implement queue/progress/reconnect/stop states in `apps/coire-web/src/hooks/useImageJob.ts` and `components/images/ImageTimeline.tsx`, including expired auth and server-authoritative terminal results (FR-007, FR-026, FR-034, SC-001, SC-010, SC-011).
- [ ] T047 [US1] Implement private gallery, authenticated thumbnail/download transport, tag filters, deletion and expired-grant refresh in `apps/coire-web/src/components/images/ImageGallery.tsx` and `styles/images.css`; keyboard and both themes follow the design reference (FR-012, FR-021, FR-022, FR-031, FR-034).
- [ ] T048 [US1] Wire image jobs/workers into admin activity, audited inspect/kill/unload routes and UI in `apps/coire-api/src/coire_api/routes/admin_images.py`, `routes/admin_console.py` and `apps/coire-web/src/pages/admin/ActivityPage.tsx` (FR-004, FR-007, FR-024, FR-033, FR-036).

## Phase 5: US2 — Exact settings, inputs and reproduction (P1)

**Independent test**: Import a generated PNG into a new form; all settings restore exactly once.
Same-environment images with retained inputs match pixels; changed environment/missing inputs
are identified. No imported metadata can grant access, load a path or acquire a model.

- [ ] T049 [P] [US2] Add bounded recipe/mask/normalization/file-worker contract tests in `apps/coire-file-worker/tests/test_image_inputs.py`, including valid 10–64 MiB generated PNG recipe round trips, rejection above 64 MiB and above 10 MiB for generation inputs, no pixel decode for recipe purpose, chunk/metadata bombs, forged IDs, orientation and mask polarity/dimension mismatch (FR-014, FR-028, FR-030).
- [ ] T050 [P] [US2] Add owner-input/import/delete API tests in `apps/coire-api/tests/contract/test_image_inputs.py` and replay/prefix/version checks in `tests/unit/test_image_metadata.py` (FR-014, FR-024, FR-028, FR-030, FR-031, SC-012).
- [ ] T051 [P] [US2] Add real-pipeline pixel/recipe round-trip test definitions in `apps/coire-node/tests/engine/test_real_image_worker.py` and import/reuse browser tests in `apps/coire-web/src/components/images/ImageMetadataImport.test.tsx` (FR-013, FR-014, FR-022, SC-002).
- [ ] T052 [P] [US2] Add mode/LoRA/control/upscale compatibility and missing-local-component tests in `apps/coire-node/tests/unit/test_image_pipeline.py`, including full-precision values and no silently ignored settings (FR-001, FR-002, FR-015, FR-030).
- [ ] T053 [US2] Extend isolated file parsing in `apps/coire-file-worker/src/coire_file_worker/image_inputs.py`, `app.py` and `security.py` for image/mask normalization and separate <=64 MiB recipe-only PNG chunk extraction with <=64 KiB uncompressed metadata and no pixel decoding; never promote a recipe asset into an init/mask/control input; keep model code off core (FR-014, FR-028, FR-030, FR-036).
- [ ] T054 [US2] Implement independent owner input storage/processing/transfer/delete in `apps/coire-api/src/coire_api/images/inputs.py`, `routes/images.py`, `file_worker_client.py` and `coire_scheduler/files.py`, reusing bounded namespaces and explicit active-reference cancellation (FR-024, FR-028, FR-029, FR-030, FR-031).
- [ ] T055 [US2] Implement safe recipe import, dependency/input rebinding and environment comparison in `apps/coire-api/src/coire_api/images/metadata.py`; restore direct fields without duplicating preset prefix, refuse missing/unauthorized assets and disclose non-exact reproduction (FR-013, FR-014, FR-028, SC-002).
- [ ] T056 [US2] Implement img2img/fill/Canny/ordered-LoRA/upscale stage adapters in `apps/coire-node/src/coire_node/image_runtime/pipeline.py` with local composite manifests, validated mask/factor semantics, measured memory bounds and matching per-kind acquisition validation before publication (FR-001, FR-002, FR-015, FR-030).
- [ ] T057 [US2] Add advanced inputs/settings and metadata drag/drop to `apps/coire-web/src/components/images/ImageForm.tsx` and `ImageMetadataImport.tsx`; show field-specific unsupported/missing/changed-environment errors and current retention limits (FR-001, FR-014, FR-028, FR-030, FR-034).
- [ ] T058 [US2] Add reuse-settings, unchanged regeneration and new-seed actions to `apps/coire-web/src/components/images/ImageGallery.tsx`, submitting fresh keys through the same native validation and preserving source input bindings (FR-014, FR-022, SC-002).

## Phase 6: US3 — Bounded stage reuse (P2)

**Independent test**:20 warm-cache seed/step changes reuse encoding; prompt/revision/adapter
changes invalidate correctly. Cache occupancy remains bounded, and one patched stack survives.

- [ ] T059 [P] [US3] Add cache identity/byte-limit/owner partition/invalidation tests in `apps/coire-node/tests/unit/test_image_cache.py`, including cross-user authorized preset reuse without source-image leakage (FR-009, FR-010, FR-024).
- [ ] T060 [US3] Implement bounded prompt/control stage caches in `apps/coire-node/src/coire_node/image_runtime/cache.py` and connect pinned encoder/preprocessor hooks, including all effective input/version/transform identities (FR-009, SC-003).
- [ ] T061 [US3] Implement one-stack clean-base LoRA replacement and peak-memory admission in `apps/coire-node/src/coire_node/image_runtime/pipeline.py`, including eviction/unload invalidation and no cumulative patches (FR-009, FR-010).
- [ ] T062 [US3] Instrument cache hit/miss/eviction/stage duration/occupancy in `apps/coire-node/src/coire_node/metrics.py` and `image_runtime/cache.py`, without prompt/owner high-cardinality labels (FR-009, FR-033, SC-003).
- [ ] T063 [US3] Add truthful cache/residency facts to `apps/coire-web/src/components/images/ImageTimeline.tsx`, including unavailable diagnostics and cold/evicted cache states (FR-009, FR-033, FR-034).
- [ ] T064 [US3] Extend `apps/coire-node/tests/engine/test_real_image_worker.py` with 20 warm-cache trials, changed-prompt/adapter misses and byte-bound checks; collect counter/span evidence using the tiny fixture and later cluster matrix (FR-009, FR-010, SC-003).

## Phase 7: US5 — Residency, placement and chat coexistence (P2)

**Independent test**: Concurrent scheduler/API processes cannot over-admit a node; unvalidated
pairs wait. An approved same-node image/chat configuration completes images while meeting chat
latency. Idle unload and crash recovery release reservations only after confirmed termination.

- [ ] T065 [P] [US5] Add cross-process admission/lease-expiry/profile-invalidation tests in `apps/coire-api/tests/unit/test_image_admission.py`, covering pinned placement, concurrent chat arrival and incompatible new loads (FR-004, FR-005, FR-006, FR-025).
- [ ] T066 [P] [US5] Add image resident/transient/TTL/re-adoption/drift tests in `apps/coire-node/tests/unit/test_image_residency.py` and scheduler ledger tests in `apps/coire-api/tests/unit/test_image_placement.py` (FR-004, FR-006, FR-029).
- [ ] T067 [P] [US5] Add a repeatable mixed-workload benchmark driver and report schema in `tests/benchmarks/image_chat.py`, recording gateway overhead, first-token p50/p95, decode throughput, image progress, thermal state and footprint (FR-033, FR-035, SC-006).
- [ ] T068 [US5] Implement shared atomic accelerator admission/profile matching in `apps/coire-api/src/coire_scheduler/image_admission.py`, `coire_api/placement/service.py` and `gateway/proxy.py`; image scheduling prefers B within registry policy and honors chat leases/pinning (FR-004, FR-005, FR-006, SC-006).
- [ ] T069 [US5] Implement measured coexistence profile validation/invalidation and latency/thermal dispatch circuit breaker in `apps/coire-api/src/coire_scheduler/image_admission.py`; add audited human-admin report admission in `coire_api/routes/admin_images.py`, refuse failing/stale reports, stop new image work and request cancellation on live regression (FR-006, FR-033, SC-006).
- [ ] T070 [US5] Implement complete resident/transient/cache/CPU-classifier memory accounting and physical-footprint reconciliation in `apps/coire-node/src/coire_node/reservations.py`, `metrics.py` and scheduler `images.py`; do not count memory twice or release uncertain live processes (FR-004, FR-009, FR-010, FR-029).
- [ ] T071 [US5] Extend idle TTL, pinning, crash/node-restart reconciliation and image-worker unload inventory in `apps/coire-api/src/coire_scheduler/placement.py`, `images.py` and `apps/coire-node/src/coire_node/agent.py` (FR-004, FR-006, FR-025).
- [ ] T072 [US5] Prove real contention gate behavior with local simulated concurrency in `tests/integration/test_image_isolation.py`; exercise approved/unmeasured/changed profiles, inference priority and withheld reservation release (FR-004, FR-005, FR-006, SC-006, SC-012).

## Phase 8: Observability, packaging and acceptance

**Goal**: Complete required release evidence across every story. These tasks do not authorize
automated operations against the real Studios; the manual matrix is performed by the operator.

- [ ] T073 Add Prometheus rule and dashboard inclusion/redaction tests in `tests/observability/images.alert.test.yaml` and `tests/test_image_observability.py`, including lean-mode alerts and absent historical diagnostics (FR-033).
- [ ] T074 Add Images dashboard and queue/cancel/storage/cleanup/worker/classifier/chat-regression alerts in `deploy/observability/grafana/dashboards/images.json` and `deploy/observability/alerts/images.yaml`; wire actual deployment inclusion and retain baseline audit/metrics (FR-033).
- [ ] T075 Add all-boundary authorization and failure injection scenarios in `tests/integration/test_image_isolation.py` and `test_image_recovery.py`, including revoked access, parser crash, classifier failure, full disk, receipt/cleanup-ack loss, cancelled partial batches and reboot recovery (FR-024, FR-025, FR-026, FR-029, FR-031, FR-032, FR-035, SC-007, SC-008, SC-011, SC-012).
- [ ] T076 Build deterministic real-mflux tiny fixture factory at `apps/coire-node/tests/engine/build_tiny_image_fixture.py`; assert <=1GB, record generated manifest, use ignored `COIRE_TEST_MODEL` storage, and prevent production selection of the test factory (FR-035).
- [ ] T077 Complete and run `apps/coire-node/tests/engine/test_real_image_worker.py` on a local Apple Silicon development Mac with the tiny fixture, denied fetches, real encoder/denoise/decode, pixel/cache tests, cancellation/re-adoption and transfer cleanup; record actual results in `specs/015-image-generation/execution-record.md` (FR-035, SC-002, SC-003, SC-007, SC-008).
- [ ] T078 Update `docs/runbooks/image-generation.md` and `deploy/compose/README.md` for acquire/inspect/kill, model licences, scopes, quotas, classification, diagnostics, backup/restore, input/output retention, expired grants and drain-before-rollback (FR-015, FR-017, FR-024, FR-031, FR-032, FR-033, FR-035).
- [ ] T079 Update `docs/ARCHITECTURE.md` and add the next available ADR under `docs/adr/` covering asset-kind additions, Studio CPU tagging, cleanup-before-publication, exact-pixel scope and measured chat admission; cite constitution compliance (FR-001, FR-011, FR-014, FR-015, FR-033, FR-036).
- [ ] T080 Regenerate and verify `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`; run unit/contract/type/lint/web checks from `specs/015-image-generation/quickstart.md` and record results without skipped required coverage (FR-035).
- [ ] T081 Run migration upgrade/downgrade, local integration, immutable node install/text/VLM smoke, compose validation, affected image builds/scans/SBOM/no-shell gates through `.github/workflows/ci.yml` and record evidence in `specs/015-image-generation/execution-record.md` (FR-004, FR-015, FR-035, FR-036).
- [ ] T082 Verify browser keyboard/focus/screen-reader/light/dark/1024px/1440px journeys against `docs/design/mockups/images.html` and record screenshots/results without user content in `specs/015-image-generation/execution-record.md` (FR-034, SC-001).
- [ ] T083 Record operator-run full-model mode/replication/offline/classifier/cancellation/residency/retention/rollback checks from `specs/015-image-generation/quickstart.md` in `execution-record.md`; generated PNGs and model files stay untracked (FR-001, FR-004, FR-011, FR-015, FR-020, FR-029, FR-030, FR-032, FR-035, SC-007, SC-008, SC-011).
- [ ] T084 Record operator-run 10 same-environment reproduction trials, 20 cache trials and 15-minute same-node chat/image benchmark in `specs/015-image-generation/execution-record.md`; failing/unmeasured configurations cannot gain coexistence approval (FR-009, FR-010, FR-014, FR-033, FR-035, SC-002, SC-003, SC-006).
- [ ] T085 Complete final native/compatible/authorization audit matrix and parent task reconciliation in `specs/015-image-generation/execution-record.md` and child PR templates; link specs, cite Principles I–VII/II-a and required reviews, and leave any unproven requirement open (FR-017, FR-018, FR-019, FR-024, FR-027, FR-035, FR-036, SC-001, SC-004, SC-005, SC-009, SC-010, SC-012).

## Dependencies and execution order

T001 precedes code in every child; T002 precedes T003. T004/T005 precede schemas T006/T007;
T008 precedes migration T009. Foundations complete before story implementation. US4's reusable
guards/audits/policy must exist before US1 admission is enabled; US1 supplies jobs/results for US2,
US3 and US5. US2 adds advanced modes/import; US3 optimizes execution; US5 validates concurrent
placement. Final release requires all stories, local tiny-engine evidence and operator cluster gates.

Within a story, write its tests first and implement in listed order. Test-writing can define
future behavior without claiming that its gate has passed. Contract regeneration accompanies
**every** schema commit, not only the explicit regeneration tasks. All deployment mounts and
settings are prerequisites in Phase 2. The acquisition validation task follows the basic worker;
advanced runtime stages extend validation for their own model kinds before publication.
Tiny-engine test definitions can precede their fixture; real execution waits for the fixture
builder in the release phase. The benchmark task creates the driver; the operator task runs it.
Concurrent image/chat dispatch stays disabled until shared admission and an approved measured
profile are implemented. The MVP can run on an isolated eligible node or simulated worker.

## Parallel opportunities

`[P]` denotes independent test files after their phase prerequisites, not permission to race shared
schema/service edits. Examples: foundations T004+T005; US4 T014+T015+T016; US1 T023+T024+T025+T026;
US2 T049+T050+T051+T052; US5 T065+T066+T067. US3 T059 can be written while US2 browser integration
is underway after US1 lands; implement caches after its tests. Schemas, db.py, registry, runtime
pipeline, App.tsx and generated contracts have one editor at a time. Separate child branches/PRs
remain dependency ordered even when test design proceeds concurrently.

## Implementation strategy and checkpoints

1. Create small child artifacts, implement shared contracts/persistence and establish US4 policy.
2. Complete the US1 txt2img vertical path with private storage, cancellation, observability and
   eligible-model acquisition. Validate its fake-worker and applicable real-runtime tests. This is
   the MVP, not completion of 015; production co-resident dispatch stays gated by measured profiles.
3. Complete US2 advanced modes/recipe reproduction, US3 cache reuse and US5 measured coexistence;
   independently exercise each story's acceptance using fixtures from the preceding vertical path.
4. Finish observability/operations and all local/cluster release evidence; reconcile child task IDs
   back to this parent. A passing subset does not justify checking unrelated parent tasks.

**Counts**: 85 tasks: setup3, foundations10, US4=9, US1=26, US2=10, US3=6, US5=8, release13.
**Format**: Each task has a checkbox, sequential ID, story label when applicable, concrete path
and requirement mapping. No implementation work is marked complete in this planning pass.

## Requirement traceability

Generated from the explicit requirement references on each task; analysis must also check semantic
coverage, not only the presence of an ID. Every task belongs to a requirement or mandated gate.

| Requirement | Tasks |
| --- | --- |
| FR-001 | T004, T006, T011, T029, T035, T044, T052, T056, T057, T079, T083 |
| FR-002 | T004, T006, T029, T052, T056 |
| FR-003 | T009, T015, T019, T020, T044, T045 |
| FR-004 | T002, T003, T007, T024, T029, T030, T048, T065, T066, T068, T070, T071, T072, T081, T083 |
| FR-005 | T006, T009, T023, T025, T030, T034, T035, T037, T065, T068, T072 |
| FR-006 | T065, T066, T068, T069, T071, T072 |
| FR-007 | T006, T023, T024, T025, T029, T032, T037, T040, T041, T043, T046, T048 |
| FR-008 | T024, T026, T032, T036, T038, T039, T041 |
| FR-009 | T059, T060, T061, T062, T063, T064, T070, T084 |
| FR-010 | T059, T061, T064, T070, T084 |
| FR-011 | T012, T026, T036, T038, T039, T079, T083 |
| FR-012 | T006, T023, T026, T038, T047 |
| FR-013 | T006, T010, T031, T038, T051, T055 |
| FR-014 | T010, T049, T050, T051, T053, T055, T057, T058, T079, T084 |
| FR-015 | T003, T005, T007, T027, T028, T033, T052, T056, T078, T079, T081, T083 |
| FR-016 | T006, T023, T042, T043 |
| FR-017 | T014, T017, T078, T085 |
| FR-018 | T014, T018, T022, T085 |
| FR-019 | T015, T019, T022, T044, T045, T085 |
| FR-020 | T016, T021, T022, T029, T083 |
| FR-021 | T016, T021, T022, T038, T047 |
| FR-022 | T047, T051, T058 |
| FR-023 | T011, T012, T026, T035 |
| FR-024 | T005, T007, T014, T017, T022, T023, T028, T036, T037, T038, T040, T042, T043, T048, T050, T054, T059, T075, T078, T085 |
| FR-025 | T006, T008, T009, T023, T024, T025, T030, T032, T034, T035, T042, T065, T071, T075 |
| FR-026 | T008, T009, T024, T025, T032, T034, T037, T040, T041, T043, T046, T075 |
| FR-027 | T014, T015, T017, T018, T019, T035, T085 |
| FR-028 | T004, T006, T010, T031, T049, T050, T053, T054, T055, T057 |
| FR-029 | T008, T009, T024, T026, T032, T036, T038, T039, T054, T066, T070, T075, T083 |
| FR-030 | T004, T006, T007, T012, T033, T049, T050, T052, T053, T054, T056, T057, T083 |
| FR-031 | T008, T009, T011, T012, T026, T035, T038, T039, T047, T050, T054, T075, T078 |
| FR-032 | T016, T021, T022, T075, T078, T083 |
| FR-033 | T013, T048, T062, T063, T067, T069, T073, T074, T078, T079, T084 |
| FR-034 | T026, T044, T045, T046, T047, T057, T063, T082 |
| FR-035 | T001, T067, T075, T076, T077, T078, T080, T081, T083, T084, T085 |
| FR-036 | T002, T005, T007, T011, T012, T015, T018, T020, T027, T028, T030, T033, T045, T048, T053, T079, T081, T085 |
| SC-001 | T023, T042, T046, T082, T085 |
| SC-002 | T051, T055, T058, T077, T084 |
| SC-003 | T060, T062, T064, T077, T084 |
| SC-004 | T014, T022, T085 |
| SC-005 | T014, T022, T085 |
| SC-006 | T067, T068, T069, T072, T084 |
| SC-007 | T026, T039, T075, T077, T083 |
| SC-008 | T026, T039, T041, T075, T077, T083 |
| SC-009 | T014, T018, T022, T085 |
| SC-010 | T023, T025, T035, T040, T046, T085 |
| SC-011 | T025, T041, T046, T075, T083 |
| SC-012 | T008, T025, T035, T041, T050, T072, T075, T085 |
