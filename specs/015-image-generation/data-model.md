# Data Model: Image Generation

All wire types live in `packages/coire-core/src/coire_core/models/` with
`ConfigDict(extra="forbid")`; persisted JSON is validated through the same versioned types.
Use existing UUIDs for users, registry assets, variants and model instances; image jobs and
processing jobs use ULID strings. Input/output/preset IDs use UUIDs. Times are UTC.

## Request and resolved specification

| Type | Fields and invariants |
| --- | --- |
| `ImageSpec` | `schema_version=1`, `model_id`, optional `variant_id`, `mode` (txt2img/img2img/fill/control), `prompt`, optional `negative_prompt`, `width`, `height`, `steps`, `guidance`, optional `seed`, `n`, ordered `loras`, optional `init_image_id`, `strength`, `mask_id`, `control`, `upscale`, `output`, `content_mode` (standard/explicit). Bounds from plan and model profile. |
| `ImageLora` | Registry model/variant IDs, finite full-precision scale within the base profile's measured range. No duplicate IDs; <=4, order significant. |
| `ImageControl` | `type=canny` initially, `image_id`, control model/variant IDs, finite strength, fixed allowlisted preprocessing parameters. Unknown types rejected. |
| `ImageUpscale` | Registry upscaler/variant IDs and supported factor (2 or 4 initially, only if backend maps it exactly); final dimensions <=4096/side and <=16 MP. |
| `ImageOutput` | `format=png`, `embed_metadata=true`; other values refused in version 1. |
| `ImageSubmitRequest` | Root-level strict optional-field `ImageSpecOverrides` plus optional preset/revision IDs (no nested spec wrapper); a model must resolve from request or preset. `Idempotency-Key` header required on native admission. |
| `ResolvedImageSpec` | Fully materialized spec, effective prompt/prefix, base and auxiliary immutable manifest digests/revisions, exact LoRA scales, normalized input digests/dimensions, preset revision/provenance, effective seeds, spec hash, execution fingerprint and pipeline version. No URLs, paths, credentials, owner identity or mutable defaults. |
| `ImageRecipe` | Schema version, resolved spec, output index/seed, decoded-pixel SHA-256 and pixel dimensions. Exactly the canonical JSON stored on the output row and in the uncompressed PNG iTXt chunk `coire.image`. <=64 KiB. |

Validation is performed after defaults/preset resolution. `txt2img` excludes source/mask/control;
`img2img` needs init+strength in (0,1]; `fill` needs init+mask with identical normalized dimensions;
`control` needs a supported control model/input. Upscale is an optional final stage. Seeds are
unsigned 32-bit integers; missing seed is generated once and batch seeds increment modulo 2^32.
Reject non-finite numeric values, duplicate adapters, unsupported negative/guidance settings,
wrong model kinds, incompatible dependency versions, bounds/alignment violations and external paths.

Reuse/import converts the resolved prompt to direct request fields without reapplying a preset
prefix; preset provenance is read-only. User changes produce a new resolved spec. Missing original
inputs require same-owner reattachment with matching normalized digest before exact regeneration.
Environment mismatch permits ordinary generation only with an explicit non-exact UI disclosure.

## Registry additions

`ModelKind`: `language_model` (default for existing rows), `image_model`, `image_lora`,
`control_model`, `upscale_model`, `image_classifier`. Source for image assets is Studio only.
Image classifier is system/admin-only and cannot be picked for generation. Auxiliary models do
not have independently routable engines. Image kinds are excluded from language/vision resolution,
failover snapshots and coding verification. `EngineBackend.MFLUX` identifies generation bases.

`ImageCapabilityProfile`: fixed backend family, supported modes/fields, dimension alignment and
bounds, steps/guidance/strength ranges, allowed adapter families/scales, supported control types,
quality-to-settings map, dependency manifest, resident/transient/cache/classifier memory estimates,
validation report/version and licence/replication status. Missing profile prevents publication.
Existing capabilities remain valid without image fields. Base and auxiliary dependencies must have
verified copies on both Studios before ready/publication. Validation records are not a coding
`verified` grant. Classifier readiness is not a user entitlement.

## Persisted entities

| Entity | Key fields and constraints |
| --- | --- |
| `ImagePresetRow` / `ImagePresetRevisionRow` | UUID, stable name/description, current revision, publication state; immutable revision number/defaults/prefix/dependency IDs/entitlement requirements, admin creator and timestamps. Unique `(preset_id, revision)`. Retirement keeps history for existing outputs. |
| `ImageJobRow` | ULID, owner UUID, originating key ID/version or browser identity, idempotency key, client-intent hash, submitted/resolved specs, state/version, attempt/fence, workflow ID, selected node/instance, reservation IDs, queue/deadline times, progress, cancellation intent, safe failure code, receipt/cleanup state, authorization snapshot, timestamps. |
| `ImageJobEventRow` | `(job_id, sequence)` unique monotonic key, typed event payload and UTC timestamp. Event ID combines job+sequence; content-free except authorized terminal image projections. Retain 24 h; current snapshot remains on job. |
| `ImageInputRow` | UUID, owner, purpose (init/mask/control/recipe), generated original/normalized storage IDs, actual byte counts/digests/dimensions, state, extracted recipe/result, processing job, quota hold, active reference count and deletion timestamps. No client path. |
| `ImageOutputRow` | UUID, job/index unique, owner, generated blob key, size/file SHA-256/pixel SHA-256, recipe JSON, content tag and classifier provenance, entitlement snapshot, publication/deletion/purge timestamps. Only succeeded non-tombstoned jobs are readable. |
| `ImageTransferRow` | Unique `(job,attempt,index)`, expected size/digest, generated staging key, state (reserved/uploading/verified/discarded), receipt, node cleanup acknowledgment, lease expiry and opaque grant hash. No grant plaintext in DB/logs. |
| `ImageDownloadGrantRow` | Grant hash, output/owner UUIDs, current access-policy version, expiry <=5 min, revoked-at. Requires current owner authentication in addition to grant. |
| `ImageQuotaRow` | Owner or global scope, held/stored bytes, pending jobs, UTC day bucket and held/consumed output count; row-lock updates, nonnegative counters. Physical deletion precedes byte release. |
| `ImageExecutionLeaseRow` | Node/job or inference request, mode, fence, expiry, heartbeat and release evidence. Admission shares node lock with gateway leases; missing heartbeat is not proof an engine stopped. |
| `ImageCoexistenceProfileRow` | Node/hardware/runtime fingerprint, exact resident chat variants, image model/mode/bounds, benchmark result and measured first-token p95, validity/status. Invalidated by changed identities/bounds or safety signals. |

Reuse `ModelInstanceRow`, node process store and `MemoryReservationRow` for image worker lifecycle;
add image status/backend fields with compatible defaults. Do not make a second independent memory
ledger. Node job journal persists job/attempt/fence, PID/create_time, resolved hash, stage, receipts,
cleanup and local deadline; it is reconciliation evidence, never the authoritative owner database.

## Identity and uniqueness

Native `(owner_id,idempotency_key)` is unique for the retained job record. Compare canonical client
intent before generating a random seed or resolving new mutable preset defaults. An existing key
with identical intent returns its original resolved job, even if a preset has since changed;
changed intent returns 409. Validate current identity before returning the receipt. A native key is
8–128 printable ASCII characters. Compatible requests without a key create a fresh UUID request
identity; clients must supply a key to obtain retry deduplication.

The attempt is initially 1; reconciliation cannot increment it and regenerate automatically. A
new user submission is a new job. Node idempotency checks job, attempt, resolved hash and fence;
a duplicate with changed content conflicts. Transfer receipts are unique per output slot and digest.
Queue position is advisory among accepted eligible jobs, recalculated under scheduler admission;
no strict FIFO or generation-time promise is implied.

## State transitions and terminal publication

| From | Allowed transitions / evidence |
| --- | --- |
| queued | reserving after current auth/dependency/quota checks; cancelling; failed on queue deadline/revocation |
| reserving | running after node launch acknowledgment and held ledger/admission; cancelling; failed |
| running | transferring when all outputs are generated; cancelling; failed on process death/deadline |
| transferring | succeeded only with every core receipt verified, node scratch cleanup acknowledged and atomic publication won; cancelling; failed |
| cancelling | cancelled only after termination/cleanup proof; remains non-publishable while partitioned |
| succeeded / failed / cancelled | immutable outcome; deletion tombstones outputs separately |

Cancel intent and publish lock the same job version. Success first returns completed on cancel;
cancel first fences all future receipt publication. A lost API/scheduler restarts observation;
a lost node re-adopts by PID/create_time or proves absence and fails the job. Never release a live
or uncertain resident reservation. Expired node-local deadlines stop execution even without core.
No partial batch is visible; failed transfers can remain private staging until bounded cleanup.

Recipe-purpose input records may hold PNGs up to 64 MiB but have no normalized image asset or generation-input reference. Their ready result is only a validated <=64 KiB recipe. Signature/chunk length/CRC checks stream within the file bound and do not decode pixel data; unsupported compressed recipe chunks are refused. Other input purposes retain the 10 MiB limit and full still-image validation. A recipe-only ID cannot satisfy init/mask/control references.

Input states: uploading -> processing -> ready | failed -> deleting -> purged. Deleting an input
referenced by active work first requests cancellation and prevents publication; durable cleanup
waits for use to end. Existing output metadata survives missing inputs and reports reproduction
unavailable. Output states: staged -> published -> tombstoned -> purged. Tombstone immediately
revokes grants and access, bytes/quota follow actual purge within 24 h.

## Classification and access

`ImageContentTag`: normal / explicit / unknown. Store classifier model/revision/processor version,
nsfw probability where available, threshold (0.5 initially), tagging timestamp and safe error code.
Policy-explicit always remains explicit even if classifier returns normal. Failure yields unknown
unless already explicit. Neither classifier nor PNG metadata grants permission. Explicit generation
snapshot records granted entitlement ID/version for audit; current entitlement is still required
for dispatch, publication and explicit downloads. Unknown and explicit never qualify for sharing.

## Migration and compatibility

Add fields with safe defaults and new tables/indexes in reversible migrations, at most one per
reviewable child PR. First revision follows the actual head (currently 0022); subsequent child
revisions depend on merged predecessors. Test existing text/VLM rows and generated clients on
upgrade. Downgrade requires admission disabled, workers drained/killed, receipts reconciled and
image tables backed up; it never silently deletes active worker state. Blob deletion is an explicit
operational action, not a destructive schema downgrade side effect. Reconcile blobs against DB on
restore and never publish orphan files. Native image routes are additive; existing `/v1` text
schemas and existing UUID foreign keys do not change.
