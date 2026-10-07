# Training, Dataset and Adapter Contracts

**Status**: Design only. Generated OpenAPI from strict `coire-core` models is authoritative at
implementation; this document is not hand-authored OpenAPI.

## Common rules

All `/api/v1/admin/*` routes require an active admin browser identity or personal/admin-scoped
API key under existing policy. Run, ops and node tokens cannot invoke them. Browser mutations
enforce exact configured Origin. Mandatory audit is transactional; denied privileged operations
are audited with safe reasons. Re-check current authority on delayed effects. No training API is
added to the stateless failover frontend or MCP.

All mutation requests carry `Idempotency-Key` (1..128 opaque characters); replay of canonical
intent returns the original result, changed intent is 409. State-dependent mutations carry
`expected_version` in a strict request model. Successful mutation increments state version;
duplicate command replay returns its prior receipt even if the resource subsequently advanced.
Lists use opaque cursors, default 25/max 100, stable timestamp+ID ordering.

Errors use existing RFC 9457 `CoireError` mapping: 401 unauthenticated; 403 forbidden admin action;
404 missing/invisible target; 409 version, duplicate name, invalid transition or conflicting input;
413 upload/recipe bound; 422 row/field/unsupported setting; 429 queue/storage/rate quota with
retry guidance; 503 unavailable transient dependency. Safe extensions contain IDs, field paths,
row numbers, capacity figures and retry hints, never paths, credentials, source content or stacks.

## Dataset surface

| Method/path | Input model / response | Behavior |
| --- | --- | --- |
| `POST /api/v1/admin/datasets` | Multipart `metadata` = `DatasetUploadRequest`; one streamed JSONL file -> 202 `DatasetReceipt` | Metadata includes name, format, provenance, analysis model/variant, split seed/fraction; upload/schema validation then durable Studio analysis |
| `GET /api/v1/admin/datasets` | Cursor -> `DatasetPage` | Immutable revision summaries and state |
| `GET /api/v1/admin/datasets/{id}` | -> `DatasetDetail` | Digest, split/provenance, readiness and bounded diagnostics; no source rows by default |
| `POST /api/v1/admin/datasets/{id}/analyze` | `DatasetAnalyzeRequest` with model/variant -> 202 `DatasetAnalysisReceipt` | New identity-specific analysis or idempotent existing receipt; no source mutation |
| `GET /api/v1/admin/dataset-analyses/{id}` | -> `DatasetAnalysis` | Full bounded statistics and state; pollable by CLI/UI |
| `DELETE /api/v1/admin/datasets/{id}` | `DatasetDeleteRequest` -> 202 `DatasetDeletionReceipt` | Blocks active/paused references; retires, asynchronously purges bytes, retains terminal provenance |

Metadata fields and limits follow data-model/plan. File bytes are an explicit binary body; metadata,
receipts, failures and analysis are typed. Accept UTF-8 uncompressed JSONL only; reject content
encoding/archive tricks. Hold aggregate upload quota before writing, verify advertised and actual
size, and clean incomplete stages. Repeated failed uploads cannot bypass global disk limits.

Analysis is asynchronous: `uploading/validating/analyzing/ready` or a safe failed status. The CLI
may wait; the browser shows loading/error/retry. Invalid row sets are atomic (no silently accepted
subset). Diagnostics return total invalid count and first <=100 row/field reasons.

Supported rows:

```json
{"text":"A plain-text training sample."}
```

```json
{"prompt":"Return the sum of 2 and 3.","completion":"5"}
```

```json
{"messages":[{"role":"user","content":"What is 2 plus 3?"},{"role":"assistant","content":"5"}]}
```

Conversation form additionally permits bounded tools and OpenAI-shaped tool-call/response fields,
normalized into canonical `Conversation`; imported IDs are validated or deterministically assigned.
Image/URL content is rejected explicitly. Dataset data is never fetched from a provenance URL.

## Training surface

| Method/path | Input model / response | Behavior |
| --- | --- | --- |
| `GET /api/v1/admin/training/recipes` | -> `TrainingRecipePage` | Versioned parameterization templates with required model/dataset bindings, not executable placeholder IDs |
| `POST /api/v1/admin/training/validate` | `TrainingSubmission` -> 200 `TrainingValidation` | Bounded syntax/registry/analysis validation, resolved preview or pending-analysis reasons; no reservation/training start |
| `POST /api/v1/admin/training/jobs` | `TrainingSubmission` -> 202 `TrainingJobReceipt` | Original YAML or form-generated YAML plus optional already-validated preview digest; transactional intent/quota/name/audit/job receipt |
| `GET /api/v1/admin/training/jobs` | -> `TrainingJobPage` | Filter by state with bounded pagination |
| `GET /api/v1/admin/training/jobs/{id}` | -> `TrainingJobDetail` | Source and resolved recipe, state/version, participants, checkpoint, metrics summary, safe reason and legal actions |
| `GET /api/v1/admin/training/jobs/{id}/events` | `Last-Event-ID` -> `TrainingEvent` SSE | Ordered replay/heartbeat or snapshot reset; disconnect does not cancel |
| `GET /api/v1/admin/training/jobs/{id}/metrics` | Cursor, limit <=2000 -> `TrainingMetricPage` | Persisted loss/progress grouped by attempt; rolled-back segments identified |
| `POST /api/v1/admin/training/jobs/{id}/pause` | `TrainingControlRequest` -> 202 `TrainingCommandReceipt` | Admin pause; requests full checkpoint, then confirmed stop; <=60 s or explicit fallback |
| `POST /api/v1/admin/training/jobs/{id}/resume` | `TrainingControlRequest` -> 202 `TrainingCommandReceipt` | Paused job only, compatible checkpoint/input/runtime and new admission required |
| `POST /api/v1/admin/training/jobs/{id}/cancel` | `TrainingControlRequest` -> 202 `TrainingCommandReceipt` | Durable cancel intent; healthy all-rank stop <=5 s; idempotent terminal result |
| `GET /api/v1/admin/training/jobs/{id}/checkpoints` | -> `CheckpointPage` | Only complete checkpoints promotable; incomplete/corrupt state labeled |
| `DELETE /api/v1/admin/training/jobs/{id}` | `TrainingDeleteRequest` -> 202 `TrainingDeletionReceipt` | Terminal only; preserve referenced lineage/artifacts, purge unreferenced logs/events/state bytes asynchronously |
| `POST /api/v1/admin/training/checkpoints/{id}/promote` | `CheckpointPromotionRequest` with output slug/version -> 202 `AdapterReceipt` | Explicit promotion validates/replicates a distinct serving artifact |

`TrainingSubmission` contains `source_yaml` and `source_kind=yaml|form`, with an optional typed
`form_spec` that must exactly agree with parsing the generated YAML. Original YAML is always
stored byte-for-byte after decoding validated UTF-8; server does not silently reformat uploaded
recipes. The receipt includes job ULID, version, queued state and event URL. Submission can be
accepted into asynchronous preflight; `ready_to_run` is never implied by receipt. Impossible
capacity/invalid template discovered there produces a terminal failure with field/node reasons.

Job transition and attempt/fence semantics are in [data-model.md](../data-model.md). Concurrent
cancel/finalize is serialized on the job version: winner determines the terminal result. A
successful cancel cannot publish final output. Explicit later checkpoint promotion is a separate
admin mutation. Replaying an original submit returns the original job even after defaults change.

Cleanup compatibility amendment (2026-10-05): checkpoint details add `state=purging` while
copy erasure is unresolved. Such a point is neither recoverable nor promotable; only both-copy
erasure permits `purged`. This additive enum requires regenerated clients; existing committed
checkpoint semantics remain unchanged. The feature's existing unconstrained state column needs
no additional migration or rewrite of historical states.

SSE envelope: `id` monotonic per-job integer, `job_id`, `attempt_id|null`, `state_version`,
`occurred_at`, `kind=state|progress|checkpoint|recovery|terminal|reset` and a discriminated typed
payload. Emit only after persistence. Heartbeat comments every 15 s; retention 7 days; an old
cursor receives one `reset` snapshot with current sequence then tails. Unauthorized reconnect
fails; an open stream closes when authorization is revoked. Prompt/data content is never emitted.
Reset snapshots contain only state/version, completed update, checkpoint/adapter identities and
safe reason codes. Original recipe YAML is obtained through the authenticated detail route,
keeping untrusted recipe comments out of event streams and telemetry.

## Existing admin Jobs training projection

`GET /api/v1/admin/console/training-activity?limit=25&cursor=...` returns
`CursorPage[TrainingActivityItem]`: ULID job identity, exact model/variant, state/version,
completed/total updates, adapter slug, latest non-rolled-back train/validation losses and summed
counted memory holds. It excludes original YAML, dataset rows and storage paths. Reads require
current human-admin authority and remain available while new training is disabled. Pagination
uses bounded opaque timestamp/ULID cursors. The existing Jobs page displays this projection and
routes its confirmed Stop through the same audited, versioned training cancel endpoint; it does
not use the generic acquisition-job DELETE route for training. Legacy UUID activity remains
compatible with its existing response/cursor shape.

## Measurement surface

| Method/path | Input model / response | Behavior |
| --- | --- | --- |
| `POST /api/v1/admin/training/measurements` | `TrainingMeasurementRequest` -> 202 `TrainingMeasurementReceipt` | Explicit admin preflight or coexistence experiment, declaring nodes, exact target multiset and recipe bounds; reserved/guarded work |
| `GET /api/v1/admin/training/measurements/{id}` | -> `TrainingMeasurementResult` | Baseline/mixed metrics, pass/failure reasons, report digest and optional profile identity |
| `GET /api/v1/admin/training/profiles` | -> `TrainingProfilePage` | Current/expired/invalidated measured capabilities; no manual pass-field editing |

Cold measurement is a separately identified admission mode: require enough conservative free
headroom and no unrelated active accelerator work on participating nodes, never evict pins.
It may evaluate a previously unmeasured declared chat combination **only** for this bounded admin
experiment with local watchdog/kill protection, not authorize ordinary training. Existing pinned
ops residency is part of the declared test. Failed measurement cannot approve a profile. Baseline
and mixed tests each run 15 minutes with real training updates; source version/parameterization/
resident changes invalidate applicability. These are durable scheduler jobs, not API-side training.

`TrainingMeasurementRequest` pins workload digest, per-target request concurrency/arrival schedule,
input-token distribution (including prompts up to 4,000 tokens), output-token bound and metric/query
version before either phase starts. Both 15-minute phases must have >=100 completed requests per
declared resident target, with the same workload. `TrainingMeasurementResult` includes per-target
counts/p95 and reproducible bounded measurement summaries. A below-floor result is `inconclusive`,
never a pass. Live evidence uses a trailing 5-minute window, >=30 first-token samples per target,
telemetry freshness <=60 s, checked every 5 s. Insufficient evidence blocks new mixed admission;
a sufficiently sampled p95 >1.5 s invalidates the profile and requests protective pause. Memory,
swap, thermal and execution-lease guards operate regardless of latency sample availability.

## Adapter management and serving

| Method/path | Input model / response | Behavior |
| --- | --- | --- |
| `GET /api/v1/admin/adapters` | -> `AdapterPage` | Includes private ready/in-progress/failed rows |
| `GET /api/v1/admin/adapters/{id}` | -> `AdapterDetail` | Provenance, manifests/copies, public selector, metrics and independent harness status |
| `PATCH /api/v1/admin/adapters/{id}` | `AdapterCurationRequest` -> `AdapterDetail` | Audited versioned publication/unpublication/display metadata; cannot edit bytes, base or verified |
| `POST /api/v1/admin/adapters/{id}/retire` | `AdapterRetireRequest` -> `AdapterDetail` | Deny new selection, drain dedicated instances through existing lifecycle, retain referenced lineage |

Extend existing `POST /api/v1/instances` and admin load/pin/unload contracts with optional
`adapter_id` coupled to the existing variant. Adapter placement is single-node only; sharded
adapter requests return a typed unsupported-placement error. Base routes keep existing behavior.

Compatible `model` accepts UUID or the strict stored pair selector. `/v1/models` returns entitled,
ready, published adapter selectors alongside base models with additive `coire_base_model_id`,
`coire_variant_id`, `coire_adapter_id`, `coire_verified` and correct pair load state. No paths.
`/v1/chat/completions`, `/v1/completions`, `/v1/messages` and native chat share resolution. No
external provider, image or VLM adapter targets in this feature. Provider adapters are not inferred
from a model's name. Unknown/invisible pairs return 404 consistently.

An optional request `coire_variant_id` selects an exact authorized base variant; adapter requests
must omit it or match the adapter's fixed variant. Responses preserve the requested public model
selector; usage records store resolved variant/adapter/engine. Run-token permitted targets and
harness verification cannot be broadened by requesting the parent UUID or another pair.
Update existing harness evaluation context/submission models with optional adapter identity and
actual resolved target, preserving old base scorecards. A successful evaluation must prove requests
hit that exact target. Training never submits a passing score itself.

Failover explicitly refuses pair selectors and filters adapter-bearing engines from base residency,
snapshot/relay selection. An old engine/node incapable of expressing exact adapter identity cannot
serve adapter commands. This exclusion is tested even when only an adapter instance of a base is warm.

## CLI contract (installed `coire_api.cli`, proposed commands)

Global `--api-url` and `--token` precede the group; prefer existing `COIRE_API_TOKEN` rather than
tokens in shell history. CLI uses the same authenticated API and shared models.

```text
coire data upload FILE --name NAME --format text|prompt_completion|conversation --model UUID --variant UUID [--seed N]
coire data list
coire data show DATASET_ID
coire data analyze DATASET_ID --model UUID --variant UUID [--wait]
coire data delete DATASET_ID
coire train recipes
coire train validate RECIPE.yaml
coire train submit RECIPE.yaml [--idempotency-key KEY]
coire train list
coire train show JOB_ID
coire train events JOB_ID
coire train pause JOB_ID
coire train resume JOB_ID
coire train cancel JOB_ID
coire train checkpoints JOB_ID
coire train delete JOB_ID
coire train measure MEASUREMENT.yaml
coire train measurement MEASUREMENT_ID
coire train profiles
coire adapter list
coire adapter show ADAPTER_ID
coire adapter promote CHECKPOINT_ID --name SLUG
coire adapter publish ADAPTER_ID
coire adapter unpublish ADAPTER_ID
coire adapter retire ADAPTER_ID
coire eval harness VARIANT_UUID [--adapter ADAPTER_UUID] --engine-version VERSION
```

Controls read the current version then submit the versioned command and report a conflict rather
than retrying a changed action silently. `--wait` polls with a finite timeout and exit codes for
refused/failed versus still running. Recipe templates have required model/data bindings; the console
generates runnable YAML only after those are selected. No shell-based remote engine controls.

## Compatibility, UI and required contract tests

Regenerate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts` for every contract
change. Snapshot tests assert current base UUID clients still parse and function. New Training UI
uses generated types, shared SSE and shell tokens. Admin-only controls are hidden/gated server-side;
empty/loading/error/expired cursor/conflict/paused/recovering states are explicit and keyboard usable.

Test every listed route/verb for allowed, denied and malformed calls; binary upload bounds and
origin/idempotency/audit failure; strict YAML/form equivalence; no source-content leaks; exact
adapter serving/verification/run-token scope; independent base/adapter warm instances; non-admin
picker and failover exclusion; current-authority revocation and state-race semantics. Simulated
engines prove lifecycle/contracts; only tiny/Studio tests establish actual numerical behavior.
