# Contract Design: Image Generation

This is design input to shared Pydantic models, not a hand-maintained OpenAPI replacement.
Implementation generates OpenAPI and `apps/coire-web/src/api/schema.d.ts` from coire-core types.
Every JSON/command/event boundary below has `extra="forbid"`; raw PNG/upload bytes have separately
typed headers/manifests. Errors use shared `CoireError` subclasses and RFC 9457 problem details.

## Authorization common to user routes

Active authenticated human user or personal API key with `images`; real `user_id` required.
Explicit generation/read additionally requires live `explicit` entitlement and key scope
`images:explicit`. Service, ops, run and identity-free emergency-admin credentials are refused.
Browser writes require exact configured Origin. Owner comparison applies on every job, event,
input, output, recipe and download; unknown/cross-owner resources return 404. No ordinary admin
ownership bypass. Revoked credentials terminate event access on the next heartbeat/recheck.
Audit admission/refusal for explicit requests and every admin mutation; do not log user content.

## Native user routes

| Method / path | Request -> response | Behavior |
| --- | --- | --- |
| GET `/api/v1/images/models` | -> `ImageModelList` | Published ready entitled generation bases and compatible published LoRA/control/upscale choices, variants, measured bounds and honest residency; no classifiers or hidden assets. |
| GET `/api/v1/images/presets` | -> `ImagePresetList` | Only currently eligible published revisions and dependency summaries. |
| POST `/api/v1/images` | `ImageSubmitRequest`, Idempotency-Key -> 202 `ImageJobReceipt` | Persist job + quota + required audit before receipt; replay returns same job; changed intent 409. |
| GET `/api/v1/images` | cursor, limit, optional state -> `ImageJobPage` | Owner jobs, newest first, stable cursor; default 25 / max 100. |
| GET `/api/v1/images/{job_id}` | -> `ImageJob` | Authoritative state/progress, advisory queue position, safe reason, result projections and latest event cursor. |
| GET `/api/v1/images/{job_id}/events` | Last-Event-ID -> SSE `ImageJobEvent` | Owner-only replay/snapshot; observer disconnect does not cancel. |
| DELETE `/api/v1/images/{job_id}` | -> 202/200 `ImageJob` | Persist cancel intent; 202 until termination proven, 200 if terminal. Does not delete completed images. |
| POST `/api/v1/image-inputs` | bounded multipart `ImageInputUpload` metadata + file -> 202 `ImageInput` | Purpose init/mask/control <=10 MiB with image validation; purpose recipe PNG <=64 MiB with metadata-only chunk parsing (<=64 KiB recipe, no pixel decode). Reserve actual quota; no URLs or recipe-to-source promotion. |
| GET `/api/v1/image-inputs/{id}` | -> `ImageInput` | State, dimensions/digests, safe failure; recipe result only to owner. |
| DELETE `/api/v1/image-inputs/{id}` | -> 202 `ImageInput` | Tombstone, cancel active references before removal, then purge/quota reclaim. |
| POST `/api/v1/image-inputs/{id}/recipe` | `ImageRecipeImportRequest` -> `ImageRecipeImport` | Only ready recipe input; validated settings, missing dependency/input list and reproducibility status. Never starts work. |
| GET `/api/v1/image-outputs` | cursor/limit/tag -> `ImageOutputPage` | Private published gallery, default 25 / max 100, policy-safe preview projections. |
| GET `/api/v1/image-outputs/{id}` | -> `ImageOutput` | Owner metadata and reuse settings; no raw storage path. |
| POST `/api/v1/image-outputs/{id}/download-grants` | empty -> `ImageDownloadGrant` | Opaque URL, expires_at; <=300 s and subject-bound. |
| GET `/api/v1/image-outputs/{id}/content` | grant query + current credentials -> PNG | Both required; exact output+owner binding, expiry/tombstone/entitlement rechecked; private/no-store. |
| DELETE `/api/v1/image-outputs/{id}` | -> 202 `ImageDeletionReceipt` | Deny reads immediately, purge within 24 h, release quota after bytes removed. |

Recipe extraction accepts the same maximum PNG size as generation output, including valid files larger than 10 MiB. Validate signature and streamed chunk lengths/CRCs; accept only the bounded uncompressed `coire.image` recipe, reject compressed recipe chunks, and never decode IDAT data. Multipart framing has its own small allowance; application limits apply to the file bytes and purpose.

`ImageRecipeImportRequest` contains schema version and a bounded map from recipe input digests to
owned replacement input IDs. Unknown schema version, corrupt recipe or executable/path fields
are rejected. Missing model versions do not trigger downloads. UI can reuse direct fields without
exact-regeneration claims, but actual submit revalidates every dependency.

## Admin routes and existing registry extension

| Method / path | Shared shape and authorization |
| --- | --- |
| POST `/api/v1/admin/image-presets` | `ImagePresetCreate -> ImagePreset`; authenticated human admin, audit, dependency validation. |
| PATCH `/api/v1/admin/image-presets/{id}` | `ImagePresetUpdate` with expected revision -> new immutable revision; 409 on stale edit. |
| DELETE `/api/v1/admin/image-presets/{id}` | Retire; audited; retain revision history. |
| GET `/api/v1/admin/image-jobs` | `ImageJobPage`; admin inspection, bounded fields, audited access. |
| GET `/api/v1/admin/image-jobs/{id}` | `ImageJobAdminView`; audit inspection, full stored recipe without direct unaudited content access. |
| DELETE `/api/v1/admin/image-jobs/{id}` | Audited kill using same cancellation arbitration. |
| POST `/api/v1/admin/image-coexistence-profiles` | `ImageCoexistenceReport -> ImageCoexistenceProfile`; audited human admin, exact runtime/model/bounds and benchmark evidence, reject missing/stale/failing latency or progress. |
| GET `/api/v1/admin/image-workers` | `ImageWorkerList`; node, model/instance, state, reserved/measured bytes, cache occupancy, active job, TTL. |
| DELETE `/api/v1/admin/image-workers/{instance_id}` | Audited drain/cancel/unload; hold reservation until node confirms death. |

Extend existing admin model acquire/publish/retire/variant contracts with image asset kind,
capabilities and dependency manifests. Existing entitlements API remains the sole grant/revoke
surface. Add image jobs to the existing admin activity view/kill adapter rather than hiding jobs
in a separate operational system. Optional recipes in `recipes/images/` contain registry binding
slots; importing a recipe creates a preset only through this admin API and never fetches weights.

## Compatible `/v1/images/generations`

POST accepts `OpenAIImageGenerationRequest`: required registry `model` and `prompt`; optional
`n` (1–4), `size` (`WIDTHxHEIGHT` or profile default), `quality` (auto/standard/hd/low/medium/high
only when an explicit model-profile mapping exists), `response_format` (url default or b64_json),
`user` (bounded untrusted client label, never authority), `stream=false`, `output_format=png`.
Coire additions: optional `coire_preset_id`, `coire_preset_revision`, `coire_seed` and
`coire_content_mode`. Optional Idempotency-Key activates the same owner-intent deduplication.
Unsupported fields/values, native advanced modes and remote-provider model IDs are field errors.
Required `model` is explicit registry selection; no hard-coded default or provider model alias.

Resolve once into the native job; no second engine, safety, accounting or blob path. A successful
HTTP200 response is `OpenAIImageGenerationResponse` with integer Unix `created`, `data` of
`{url}` or `{b64_json}` entries, and additive `coire_job_id`. Do not fabricate provider token usage
or revised prompts. PNG payloads include the same recipe as native downloads. URL requires current
authentication; base64 is recommended for clients lacking authenticated image-download support.

Wait <=90 s. Timeout returns safe HTTP504 problem details containing `coire_job_id`; accepted work
continues, including if the caller disconnects. Owners can inspect/cancel via native routes or
repeat the same key. Do not return a native queue receipt as compatible HTTP200/202 success.
Refusals before admission create no job; completed failed jobs return a safe error with job ID.

Upstream success shape reference:
[OpenAI image generation](https://developers.openai.com/api/reference/resources/images/methods/generate).

## Event contract

`ImageJobEvent` includes `job_id`, positive `sequence`, UTC `at`, `type`, typed payload.
SSE `id` is `<job_id>:<sequence>`; event name matches type; payload is one JSON shape.

| Event | Payload |
| --- | --- |
| queued | Advisory position and waiting reason (capacity/chat/provisioning), current state. |
| started | Node name, instance ID and immutable attempt identity. |
| progress | Stage, output index, completed step/total when known, timings; no speculative percentage or partial PNG. |
| done | All published `ImageOutput` projections after receipts and cleanup; exactly one terminal outcome. |
| error | Safe code/detail and `failed` state; no engine traceback. |
| cancelled | Confirmed `cancelled` state and no outputs. |
| reset | Full current `ImageJob` and latest cursor after expired event retention. |

A stale valid cursor receives reset; a malformed cursor or different job prefix returns 400.
Heartbeat comments at 15 s. Progress persisted at most 4 Hz/job; terminal events never dropped.
Out-of-order/duplicate browser delivery is ignored by sequence, and terminal snapshot wins.
Event stream auth is rechecked at least every 15 s. Client uses the shared `useEventStream` transport,
not a new parser/direct fetch. Histories contain no grant URLs that outlive current authorization.

## Node and worker boundaries

All node routes require existing node-control authentication plus expected node/job/attempt/fence.
Worker loopback transport uses a short-lived per-worker secret and strict `ImageWorker*` models;
only coire-node reaches it. Loopback does not imply trusted callers.

| Boundary | Request -> response |
| --- | --- |
| Existing engine start/status/stop | Extend `EngineStartRequest`/status for MFLUX instance with verified local manifest and memory reservation; no client-supplied import/class/path. |
| PUT node `/images/jobs/{job_id}` | `NodeImageStartRequest -> NodeImageJob`; immutable resolved spec, owned transferred input manifest, attempt/fence, deadline, reservation; identical replay attaches. |
| GET node `/images/jobs/{job_id}` | `NodeImageJob`; journal, progress cursor, PID/create_time, staged output receipts and cleanup status. |
| DELETE node `/images/jobs/{job_id}` | `NodeImageCancelRequest -> NodeImageJob`; idempotent fence/cancel, TERM/KILL deadline. |
| POST node `/images/jobs/{job_id}/cleanup` | `NodeImageCleanupRequest -> NodeImageCleanupReceipt`; accept only matching verified core receipts; remove scratch and acknowledge. |
| Worker load/generate/status/cancel/unload | `ImageWorkerLoadRequest`, `ImageWorkerRunRequest`, `ImageWorkerStatus`, `ImageWorkerCancelRequest`, `ImageWorkerUnloadRequest`; all shared, no arbitrary call/method names. |
| PUT node `/images/jobs/{job_id}/inputs/{input_id}` | `NodeImageInputRequest` typed attempt/fence/size/digest headers plus PNG stream; node-control authentication, per-attempt input binding, generated paths only. |
| PUT internal `/internal/images/{job_id}/outputs/{index}` | Node identity + grant + PNG stream -> `ImageTransferReceipt`; exact size/digest, active attempt, identical retry, conflict on replacement. |

The scheduler mints grants through shared domain code and its existing database authority,
persisting only their hashes; no HTTP grant-minting endpoint or new scheduler bearer is introduced.
`ImageTransferGrantRequest` and `ImageTransferGrant` remain typed command/result records carried
through node dispatch. API upload validates the node-bound grant against its stored attempt, node
identity and fence; the narrow upload credential never authorizes user image routes. Renew only
while the same attempt remains live. Expired transfer leaves private staging for reconciliation,
never retriggers denoising. Node status and journals contain no full prompt in unscoped telemetry.
The isolated CPU file-worker contract is extended with versioned image operations and strict
request/result models in `models/files.py`; scheduler remains its sole dispatcher.

## Error and race matrix

| Condition | Outcome |
| --- | --- |
| Missing/invalid/revoked credentials | 401; no generation/bytes; explicit refusal audit when identity is known. |
| Missing scope/entitlement or unsupported principal | 403 and required refusal audit. |
| Unavailable/cross-owner resource | 404 without disclosure; retired dependency named safely for its owner/admin. |
| Changed idempotent body/stale preset revision | 409. |
| Invalid mode/settings/metadata | 422 with field and bound; no node load. |
| Quota/rate/pending limit | 429 with bounded Retry-After; no double hold. |
| Insufficient blob safety capacity | 507 before admission; mid-transfer failure cleans private staging. |
| Cancel before publication | No download; 202 until death/cleanup confirmed, then cancelled. |
| Publication before cancel | Return already-succeeded snapshot; output deletion is separate. |
| Model/entitlement changes while queued | Refuse dispatch, safe terminal reason and audit; release unstarted budget/holds. |
| Lost node/unknown process state | Hold reservations, fence publication, reconcile; no blind retry. |
| Classifier timeout/failure | Publish owner-private unknown tag with diagnostic, unless policy already explicit. |

## Compatibility and verification

Generated contract tests cover every route/method, shared node/worker message, file-worker operation,
SSE variant and compatible response. Schema additions regenerate OpenAPI and TS in the same child
commit; adapters do not hand-maintain response interfaces. Existing text/VLM/MCP/failover tests
must prove image assets cannot enter those routing surfaces. No CORS/network policy widening.
