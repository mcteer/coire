# Implementation Plan: Image Generation

**Branch**: `feat/015-image-generation` | **Date**: 2026-09-30 | **Spec**: [spec.md](spec.md)
**Input**: `specs/015-image-generation/spec.md`; baseline `origin/main` at `ff69d75`.

## Summary

Add private image generation through a typed native job API and the Images shell. The API
persists authorized work, the scheduler owns durable execution, and coire-node owns a resident
bare mflux process on a Studio. Core stores completed PNGs and metadata. Registry capabilities
determine supported settings; all entry points share admission, cancellation and publication.

## Technical Context

**Language/Version**: Python 3.13; existing React/TypeScript strict workspace.
**Primary Dependencies**: Existing FastAPI, Pydantic v2, SQLAlchemy/Alembic, httpx, DBOS and OTel.
Add Darwin-only `mflux==0.20.0` (MIT); resolve exact transitive versions in `uv.lock` and extend
the frozen wheelhouse installer. Existing Studio Transformers/PyTorch executes the auxiliary
classifier on CPU. Existing isolated CPU file worker owns Pillow. No new web library/service.
**Storage**: Postgres 17 and new API-owned `coire-blobs` volume. Isolated preprocessing uses new
namespaces on existing file-worker original/derived volumes, without access to final gallery blobs.
**Testing**: pytest unit/contract, Postgres integration, Vitest/Testing Library, browser acceptance,
local Apple Silicon tiny mflux fixture, operator full-model cluster matrix.
**Target Platform**: Existing hardened core containers; native macOS 26.2+ Studio runtime.
**Performance Goals**: Native receipt <=1 s p95; progress <=2 s; healthy cancel <=5 s; loaded chat
first token <=1.5 s p95 for <=4k prompts during 15-minute supported mixed workload. Preserve
existing <=20 ms p95 gateway overhead excluding model work.
**Constraints**: One image executor per Studio, bounded memory/caches/disk, offline execution,
owner-only access, personal scoped keys, no image tools on MCP, no learned inference on core.
**Scale/Scope**: Two Studios; four outputs/request; four pending jobs/owner; 32 globally;
gallery pages default 25/max 100. All high-level modes require tested per-model capabilities.

## Constitution Check

Pre-research and post-design: **PASS for the design against constitution 2.0.0**. Implementation
checks and measured hardware acceptance remain future tasks, not claimed results.

| Principle | Compliance and required evidence |
| --- | --- |
| I — Bare engines | Direct mflux APIs in node-owned native processes; no wrappers/graphs; explicit argv, authenticated loopback IPC, complete offline dependencies. |
| II — Core/worker split | Generation and model-based classification only on Studios. Core handles orchestration, storage and bounded CPU parsing. |
| II-a — Containers | Extend existing single-process services and isolated CPU file worker; retain distinct hardened images, non-root, read-only rootfs, dropped caps, networks, healthchecks, scans/SBOM. |
| III — Contracts first | Strict shared models before services; generated OpenAPI/TS; standard compatible fields plus additive `coire_` extensions. |
| IV — Zero trust | Owner identity, current scopes/entitlements, origin checks, audited mutations, short-lived scoped transfer/download grants and admin kill. |
| V — Models as data | Registry resolves every dependency; only admin acquisition; verify both Studio copies before publication; never fetch weights during generation. |
| VI — Observable | Content-free logs, spans, metrics, dashboard and tested baseline alerts; disclose optional history availability. |
| VII — Spec/test gated | Tests first, reviewable child specs/branches, <=1 GB local engine fixture and operator cluster evidence before feature completion. |

No constitution exception. An implementation ADR records image asset kinds, Studio CPU tagging,
publication/cancellation rules and measured coexistence policy without changing those principles.

## Project Structure

### Documentation

`specs/015-image-generation/{spec.md,plan.md,research.md,data-model.md,quickstart.md,tasks.md,
checklists/requirements.md,contracts/images.md}`.

### Source changes during implementation

All paths are repository-relative. New modules are proposed paths, not existing capabilities.

| Concern | Paths |
| --- | --- |
| Contracts | `packages/coire-core/src/coire_core/models/{images,image_worker,registry,engine,acquisition,files,console}.py`, `errors.py`, `settings.py` |
| Persistence | `apps/coire-api/src/coire_api/db.py`; next Alembic revision after `0022_stopped_usage_outcome` (recheck per child PR) |
| Routes/domain | `apps/coire-api/src/coire_api/routes/{images,v1_images,admin_images}.py`, `app.py`, `openapi.py`, `auth.py`; new `images/{service,authorization,presets,storage,inputs,events,metadata,maintenance,telemetry}.py` |
| Scheduling | `apps/coire-api/src/coire_scheduler/{images,image_admission,main,workers,placement}.py`; `coire_api/{image_executor,nodes_client}.py` |
| Acquisition | `coire_api/registry/{inspection,acquisition,acquisition_executor,variants,service}.py`; `coire_node/{hub,worker,validation,image_validation}.py` |
| Worker | `apps/coire-node/src/coire_node/{engines,agent,store,reservations,metrics,image_worker,image_jobs}.py`, `routes/images.py`, new `image_runtime/{pipeline,cache,metadata,classification}.py` |
| Chat admission | `apps/coire-api/src/coire_api/{placement/service.py,gateway/proxy.py}`; scheduler `image_admission.py` |
| Parsing | `apps/coire-file-worker/src/coire_file_worker/{app,processor,security,image_inputs}.py`; API `file_worker_client.py`, scheduler `files.py` |
| Web | `apps/coire-web/src/{App.tsx,components/AppShell.tsx,pages/Images.tsx,api/images.ts,hooks/useImageJob.ts,styles/images.css}`; `components/images/{ImageForm,PresetRail,ImageGallery,ImageTimeline,ImageMetadataImport,PresetEditor}.tsx`; shared `useEventStream` |
| Packaging | `apps/coire-node/{pyproject.toml,install.sh,install_runtime.py}`, `scripts/{build-node-wheel.sh,stage-node-wheels.py}`, `uv.lock`, compose and nginx config |
| Operations | `deploy/observability/{alerts/images.yaml,grafana/dashboards/images.json}`, `deploy/compose/README.md`, `docs/runbooks/image-generation.md`, `docs/ARCHITECTURE.md`, `recipes/images/` |

**Structure decision**: Scheduler source lives in `apps/coire-api/src/coire_scheduler/` and runs
in its separate image/process. `coire_node/engines.py` is a file; new runtime helpers belong in
`image_runtime/`. Existing registry/variant/instance IDs remain UUIDs; new image jobs use ULIDs.

## Phase 0 — Research

[research.md](research.md) records verified source behavior, licences and alternatives. No product
clarification is outstanding. Lock resolution, engine probes and performance measurements remain
implementation gates. This plan does not claim they passed.

## Phase 1 — Design

### Registry, capabilities and presets

Add a backwards-compatible registry `kind` defaulting to `language_model`, four image kinds and
an admin-only `image_classifier` auxiliary kind. `EngineBackend.MFLUX` applies only to generation
bases. Auxiliary assets cannot become chat targets. Image capabilities name supported modes,
field bounds, adapter compatibility, component manifests and measured working-memory estimates.
Existing chat/VLM behavior and UUID foreign keys remain compatible.

Presets have immutable revisions and typed defaults. Resolve model defaults < preset defaults <
explicit fields, then apply the recorded prefix exactly once. Never clamp silently. Required
entitlements are the union of request mode, preset and all selected dependencies. Recheck current
publication/authorization before dispatch and publication; accepted settings never mutate when a
preset changes. Acquisition inspects image-specific layouts and all auxiliary dependencies,
validates on a reserved Studio, verifies both copies, and records model licences. Generation
receives a complete local manifest, offline settings and no HF credential.

### Identity, quotas and durable execution

Add an image-specific guard: active USER/ADMIN with a user ID, or a personal API key with `images`.
Explicit work additionally requires current `explicit` entitlement and `images:explicit` on a key.
Reject service, ops, run and identity-free emergency credentials. Generic `require_scope` alone
is insufficient. Browser mutations require the configured exact Origin. Ordinary reads stay owner
scoped even for admins; separate admin inspection/cancel routes are audited.

Admission checks `(owner,idempotency_key)` against canonical client intent before new default/seed resolution, locks owner/global quota rows, resolves a new request,
reserves storage, records audit, job and first event in one short transaction. Identical retry
returns the same receipt; changed effective settings conflict. Store key ID/version, never its
secret. Recheck authorization during execution and immediately before publication; revocation
cancels explicit work. Mandatory audit failure refuses the mutation. Prompts/images stay out of
logs/audit. Use a separate default allowance of 100 accepted output images/owner/UTC day; queued
cancellation releases it, started work consumes it even on failure. Existing key request limits
still apply; image work does not fabricate token usage.

Only the scheduler registers DBOS image workflows. Deterministic workflow IDs, persisted command
IDs and attempt fencing prevent duplicate generation. Node start is idempotent by job/attempt;
its journal records intent before work. Recovery queries `(pid,create_time)` and the journal,
resuming observation/transfer/publication rather than re-running an uncertain request. A dead
worker with no recoverable result fails visibly; retry requires a new explicit submission.

State flow is `queued -> reserving -> running -> transferring -> succeeded`, with
`cancelling -> cancelled` and `failed` exits. Lock job version to arbitrate cancel versus publish.
A successful cancel can never publish; an already successful job returns its completed state.
Observer disconnect never cancels a native job. A partition holds cancellation and reservations
pending proof of termination, while fencing refuses output; node enforces the deadline locally.

Persist events before SSE emission. Replay with `Last-Event-ID`; after 24-hour retention send a
reset snapshot/current cursor. Keep terminal snapshots independently. Heartbeats every 15 seconds;
no image previews from unfinished jobs. GET reconnect never resubmits generation.

### Worker lifecycle and pipeline

Use one lazily loaded resident Python mflux worker per Studio and one job at a time. Extend the
existing engine manager/store/re-adoption and instance inventory for the MFLUX backend. Launch
with explicit argv in a separate process group, loopback auth and registry-only paths. Record PID,
create time, port and reservation before returning; a warm probe establishes readiness.

Implement txt2img/img2img, fill, Canny control, ordered LoRA stacks and upscale with the specific
mflux classes documented in research. Other controls fail until separately acquired and validated.
Every high-level mode needs real-model acceptance before 015 is complete. Turbo rejects unsupported
negative prompts/nonzero guidance. LoRA replacement reloads a clean base, not cumulative patches.

Synchronize lazy MLX work before reporting a completed step. Cooperative cancellation escalates
through node TERM/KILL within five seconds, including stuck encoders/decoders; hard kill loses
residency. Hold resident memory until process death. Successful jobs release transient reservations
and keep the bounded idle worker reservation; default idle TTL 15 minutes, admin pin override.

### Memory, stage caches and chat coexistence

Reserve resident weights/caches/classifier plus worst-case transient buffers/activations. Reconcile
physical footprint and retain the existing >10% drift alert. Bound prompt cache to 256 MiB, control
tensors to 256 MiB and patched weights to one stack within the model reservation. Replacement peak
must fit before loading. Cache identity includes immutable component/runtime/dtype revisions,
effective encoding inputs, full-precision ordered adapter scales, and preprocessing transforms.
Seed/steps are excluded only when irrelevant to that stage. Clear on unload/revision change.
Shared authorized preset encodings may cross owners; user image-derived cache entries are owner
partitioned. No retrievable raw input/output image bytes persist in caches.

Add atomic per-node accelerator admission used by both gateway request leases and image dispatch.
A measured coexistence profile identifies node/runtime, resident chat variants and permitted image
bounds. Image jobs wait or choose another eligible Studio if any resident combination is unmeasured;
prefer B, but never violate explicit pinning. Incompatible new model loads wait while image work
holds admission. Approved resident chat keeps priority and continues. Version change, thermal
alarm or latency regression disables further image dispatch; a live regression requests image
cancellation and alerts. Serialization alone is insufficient. The required 15-minute hardware
gate must demonstrate both chat latency and actual image progress in an approved same-node pair.

### Inputs, publication and reproducibility

Independent image inputs have owner records rather than fake chat conversations. Extend the
isolated file worker with typed normalize-image, normalize-mask and extract-recipe operations
using its existing network/auth and dedicated path namespaces. Bound uploads and decoded pixels;
reject animation and malformed generation inputs. Recipe-only PNG uploads have a separate 64 MiB cap and bounded chunk validation, without decoding or normalizing pixels; extract at most 64 KiB of uncompressed `coire.image` metadata. Reject oversized/truncated chunks and never expose these uploads as source-image assets. Preserve grayscale mask meaning: white edits, black keeps.
Extract only the bounded `coire.image` recipe before normalization strips metadata; revalidate
it as untrusted settings. No imported paths, privilege claims or network URLs are followed.

Scheduler mints persisted node/attempt-scoped transfer grants through shared database domain code; node streams PNGs with those short-lived grants to API staging on
`coire-blobs`. Verify size, SHA-256 and canonical recipe; publish the whole batch in one transaction
only after all files are durable, authorization is current and cancellation has not won. Stream
rather than buffering batches. Stage uploads idempotently; no overwrite with different bytes.
Require node scratch-cleanup acknowledgment before the job becomes succeeded and downloadable.
A core restart reconciles durable staging, cleanup acknowledgments and publication. Node restart
removes abandoned input/output files before readiness; core sweeps orphans and stale quota holds.

Write canonical Coire JSON in an uncompressed PNG iTXt chunk named `coire.image`, avoiding upstream rounding/local paths. Record exact settings,
immutable versions, input digests, environment fingerprint and each seed; exclude identity/secrets.
Resolve missing seed once, output `i` uses `(seed+i) mod 2^32`. Compare decoded pixel digests, not
PNG file bytes. Same-environment regeneration requires original source inputs; missing inputs
must be reattached with matching digest. Changed runtime/hardware explicitly disables the exact
reproduction claim. The user confirmed that old environments need not be preserved.

Studio CPU tagging uses the pinned admin-acquired classifier in research. Failure/timeout produces
`unknown` plus diagnostics, without blocking entitled output. Record `explicit` if either policy
or classifier marks it. No shared/public gallery exists; explicit and unknown can never be shared.
Downloads require a live owner identity plus an opaque five-minute grant; explicit content also
requires current explicit entitlement/scope. Deletion/revocation invalidates access immediately.
Use private/no-store caching and keep signed URLs out of logs.

### Configured defaults

All runtime settings live in `coire_core/settings.py`, documented as environment variables in
compose README. Model/entitlement limits may only tighten the global values.

| Bound | Default |
| --- | --- |
| Generation-input upload / pixels | 10 MiB / 20 MP; PNG/JPEG/WebP stills; normalized <=2048/side and <=4 MP |
| Recipe-only upload | PNG <=64 MiB total, matching output cap; metadata <=64 KiB; bounded chunk parsing without pixel decode/normalization |
| Generation / upscale | <=2048/side and <=4 MP; upscale <=4096/side and <=16 MP; PNG <=64 MiB/output |
| Steps / count / adapters | 1–100 / 1–4 / <=4 ordered LoRAs; per-model alignment and support checks |
| Prompt / recipe | 16 KiB UTF-8 per prompt / 64 KiB recipe; no embedded source images |
| Queue / deadlines | 4 pending/owner, 32 total; queue 30 minutes; execution 10 minutes |
| Disk / daily allowance | 5 GiB/owner, 50 GiB total, 2 GiB safety floor; 100 outputs/owner/day |
| Disk hold | Worst-case 64 MiB times output count plus metadata and held input bytes |
| Retention | Outputs until deletion by default; abandoned inputs/events 24 h; core purge <=24 h |
| Transfer / download | Transfer lease 60 s, renewed while live; download grant 300 s |
| Classifier timeout | 10 seconds; expiry yields unknown without failing generation |

Compatible `/v1/images/generations` returns a standard synchronous result for its supported subset.
After a 90-second wait ceiling return 504 with `coire_job_id` for the still-durable job; same-key retry
cannot create another job. `b64_json` supports clients unable to authenticate image URL requests.
See [contracts/images.md](contracts/images.md) for exact boundaries.

### Observability and operations

Spans use `coire.api.image.*`, `coire.scheduler.image.*`, `coire.node.image.*` for admission, queue,
load, encode, denoise, decode, classify, transfer, publish and cancel. Metrics prefix `coire_image_`:
queue depth/wait, outcomes, stage time, cache hits/misses/bytes, reservations/footprint, storage,
cancellation and cleanup. Bounded stage/outcome/node labels only; IDs in structured logs/spans.
No prompt, PNG, recipe, secret or signed URL values in telemetry.

Add an Images dashboard and alerts for no queue progress >5 minutes, cancellation overrun,
storage safety floor, cleanup failure, repeated worker death, unknown tagging and chat regression.
Metrics/audit/alerts work in lean mode; trace/dashboard controls disclose disabled diagnostics.
Runbook covers acquire, inspect, kill, limits, retention, backup, drain-before-rollback and paired
DB/blob restore. Extend existing networks/mounts; do not widen CORS/firewalls/capabilities.

## Delivery and Verification

015 exceeds CONTRIBUTING's reviewable-PR size. Before coding each increment, create a small
`015a`, `015b`, ... child spec/plan/tasks and branch from updated main, tracing parent task IDs.
Suggested boundaries: contracts/registry; persistence; auth/presets; inputs; worker/acquisition;
durable jobs; publication/gallery; metadata/modes; caches/coexistence; operations/acceptance.
Split further at about 800 non-generated lines. One reversible migration per child PR; never
edit merged migrations. This parent planning branch contains no implementation.

Tests precede changes, then core contracts, services, generated OpenAPI/TS and web. MVP includes
foundations, US4 authorization and US1 private txt2img; all remaining stories are required for
parent completion. [quickstart.md](quickstart.md) defines gates, including real local engine and
operator cluster evidence. No test skip is counted as completion.

## Complexity Tracking

No principle exception. Jobs, inputs, preset revisions, publication state and leases are necessary
for ownership/cancellation/recovery. Reuse existing services instead of adding a broker or blob server.
