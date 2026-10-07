# Data Model: SFT Training Jobs

**Status**: Design contract, 2026-10-03. Python wire types will be implemented in `coire-core`
before service code; names here are proposed. Every wire model forbids unknown fields.

## 1. Identity and compatibility

- Preserve existing model, variant, engine and instance UUIDs. Training job/attempt IDs are ULIDs;
  new dataset revision, analysis, checkpoint, adapter and command identities are UUIDs.
- Public `ModelSelector` is either the existing model UUID or an exact registered
  `<model-uuid>@<adapter-slug>` (slug `[a-z0-9][a-z0-9-]{0,62}`). No `/`, `..`, URL, HF ID or path.
- `InferenceTarget`: `model_id: UUID`, `variant_id: UUID`, `adapter_id: UUID | null`, immutable
  base/adapter manifest digests. Internal equality, cache/coalescing and verification use this
  tuple; a nullable adapter does not mean any adapter. Adapter public selector fixes the variant.
- Optional public `coire_variant_id` narrows a base request; it cannot disagree with a selected
  adapter or token-authorized target. Existing base-only callers need no new field.
- Add nullable variant/adapter identity to usage and exact target lists to run credentials/results.
  Old model-only run scope authorizes base-only inference, never an adapter.

## 2. Canonical conversations and training examples

Extend `ConversationMessage` compatibly: optional `tool_calls` with unique call IDs, function name
and JSON-object arguments; optional `tool_call_id` for responses; bounded metadata on conversation
and messages. Existing text/image parts remain unchanged. An assistant may omit text parts only
when it contains tool calls. Tool responses reference a preceding unresolved call; duplicate,
orphaned or unresolved context relationships fail dataset validation. A final assistant tool call
may be the supervised target itself; it is a label, not an executed call awaiting a result.
Tool definitions are bounded
JSON schemas, never executable code, with max 32 tools and 64 KiB total schema/metadata per row
within the 1 MiB row limit. Reject reserved authorization/runtime keys in imported metadata.

`TrainingExample`: canonical conversation, `content_mode=text|chat`,
`loss_policy=all_tokens|final_assistant`, canonical content digest and source row reference.
Text loader creates a single assistant text message with `content_mode=text`, encoded without
chat-template framing. Prompt/completion loader creates user/assistant turns. Conversation loader
preserves roles/tool relationships, ending in an assistant target. Image parts are refused here,
not removed from the shared chat representation. Metadata/IDs/timestamps do not affect duplicate
content hashes or model input. Final-assistant masking requires verified token-prefix alignment.

## 3. DatasetRevision, DatasetAnalysis and SplitManifest

| Entity | Fields / relationships |
| --- | --- |
| DatasetRevision | UUID, admin owner, display name, `type=sft`, loader format/version, state, source digest/bytes, generated private storage key, provenance source/licence note, row count, canonical digest, split seed/fraction, split manifest digest, timestamps/version, safe failure code |
| DatasetAnalysis | UUID, dataset revision FK, model/variant FK, tokenizer/template/runtime digests, state, token histogram/min/p50/p95/max, role counts, exact duplicate groups/count, invalid/overlength/zero-target counts, timestamps/error; results immutable |
| SplitManifest | Dataset FK, schema/algorithm version, seed, canonical content-hash groups and train/validation row-index membership, manifest digest |
| DatasetReference | Job FK, dataset revision FK, split manifest and analysis IDs/digests; blocks purge while job nonterminal |

Dataset lifecycle: `uploading -> validating -> analyzing -> ready`; invalid content -> `failed`;
analysis infrastructure failure -> `analysis_failed` (explicit analyze retries create a new analysis);
`ready -> retired -> purged`. Retired data cannot enter new work. Active/paused references prevent
retirement/purge; terminal references retain metadata and get `reproducible=false` after purge.
Upload failure discards private staging within 24 hours. No in-place content replacement: replacement
creates a new UUID. Reanalysis does not change the source/split or rewrite old analysis.

Unique `(dataset_revision_id, analysis_identity_digest)` may return a completed analysis for an
identical retry; a requested fresh run has a new command identity. Statistics never expose original
row text in logs or unauthorized responses. Counts are exact; token distributions are bounded
histograms plus exact extrema and declared percentile method (fixed token buckets).

## 4. TrainingSpec and resolved intent

| Block | Version-1 fields and constraints |
| --- | --- |
| `schema_version` | Literal `1` |
| `model` | `model_id`, `variant_id`; ready local language model, exact compatible manifests |
| `data.train` | `datasets[]`: revision ID, train split, positive `sample_count`, positive `mixture_proportion`; `epoch_samples`, `mixture_strategy=weighted|sequential`, `replacement` default false, seed |
| `data.validation` | Explicit revision IDs/validation splits, max validation batches, seed; must not overlap training canonical content hashes |
| `data.loss_policy` | `all_tokens` only for text; `final_assistant` for prompt/completion/conversation; mixed incompatible policies fail or declare policy per source explicitly |
| `objective` | Literal `sft`; registry reserves extension boundary, not accepted future literals |
| `parameterization` | `kind=lora|qlora|dora`, rank 1..128, scale >0, dropout [0,1), explicit approved target module names, positive trainable layer count within base bounds |
| `optim` | `name=adam|adamw`, positive finite learning rate, beta1/beta2 [0,1), epsilon >0, weight decay >=0, `updates` 1..100000, `batch_size` 1..64 global, `accumulation_steps` 1..64, `max_sequence_length` 2..8192; constant/warmup-linear schedule and warmup <= updates |
| `eval` | Held-out `loss_every_updates` >=1 and `at_end=true` default; no task/judge suite fields accepted |
| `output` | Unique adapter slug, `checkpoint_every_updates` >=1, `keep_last_checkpoints` 1..3 initially, within configured count/bytes limits; no arbitrary filesystem output |
| `placement` | `mode=single|data_parallel`; optional declared Studio preference for single; data parallel requires both declared ranks and global batch divisible by 2 |
| `seed` | Integer 0..2^32-1 including zero |

All sources in a mixture share supported content/loss policy or carry validated per-example masks.
`sample_count` caps selected source pool rows; largest-remainder rounding of
`epoch_samples * proportion` determines per-source epoch draw quotas. Replacement false rejects
draws beyond the selected pool. Rank partitioning of the deterministic global batch is explicit;
no dropped tail or implicit sequence truncation. `epoch_samples` must be divisible by global
batch size, or preflight refuses and explains the remainder.

Resolved spec adds exact input/analysis/split digests, tokenizer/effective-template identity and
explicit thinking kwargs, runtime/worker version, per-rank resource envelope, resolved node/rank
mapping once admitted, registered capability/evidence IDs, and canonical sampler algorithm version.
Original YAML and canonical client intent remain separate from this immutable resolution.

## 5. TrainingJob and TrainingAttempt

**TrainingJob**: ULID, actor/key ID and authorization version (no credential), original YAML + hash,
canonical intent + hash, resolved spec + hash, output name reservation, state/version, pause origin,
queue reason/deadline, cumulative execution/deadline, latest committed checkpoint, successful
adapter ID, recovery count, timestamps, safe failure details, reproducibility status.
Unique `(actor_id,idempotency_key)` and live `(base_variant_id,output_slug)` reservation. Output
slug conflicts across variants of the same model also conflict because the public selector is unique.

**TrainingAttempt**: ULID, job FK, monotonically increasing generation/fence, runtime and world size,
resume checkpoint ID, execution lease, state, start/stop reason and timestamps. Unique `(job,generation)`;
one active generation per job. Every node command/event includes attempt and fence.

**TrainingParticipant**: attempt FK, declared node ID/rank, full memory/disk reservation IDs,
command intent hash/ID, spawn nonce, PID/create-time observation, authenticated control identity,
lease status, last progress/checkpoint digest, stop proof and timestamp. Unique `(attempt,node)` and
`(attempt,rank)`. Node observations cannot advance a stale generation.

### Job transitions

| From | To / guard |
| --- | --- |
| queued | preflighting; or cancelled/failed for authorization, timeout or impossible intent |
| preflighting | reserving when analysis/runtime/bounds verified; queued with temporary reason; failed on invalid/impossible work |
| reserving | running only after all participant holds/preparation/evictions and starts confirmed; recovering or failed on partial launch |
| running | pausing, recovering, cancelling, finalizing; failed only after safe teardown/reconciliation |
| pausing | paused after complete checkpoint and confirmed death; recovering while liveness/commit uncertain; forced-stop fallback records prior checkpoint |
| paused | queued on permitted explicit/automatic resume; cancelling; admin pause never auto-resumes |
| recovering | queued after stop/fence proof and selected compatible checkpoint; cancelling; failed after exhausted recovery policy and teardown |
| finalizing | succeeded only after smoke, copies and authorization verified; cancelling/failed on lost race or invalid artifact |
| cancelling | cancelled only when participant termination/fencing established; remains cancelling with reason while uncertain |
| succeeded / cancelled / failed | Terminal; read/delete metadata subject to references, explicit checkpoint promotion allowed. A new submission creates a new job; no resurrection. |

Attempt failure is not necessarily job failure. A protective forced stop may return a resumable
paused job at its last complete checkpoint; it must not claim the lost partial update was saved.
At most three automatic attempts; exhausted recovery becomes failed after safe teardown.

`EvictedTargetIntent` stores exact target/instance/policy/version, victim reservation and observed
pin state, reason and restoration state. It is an offer-to-reload record, not authority to undo
later admin changes. Training has distinct `TRAINING` reservations; do not reuse model-instance
failure cleanup to release them.

## 6. Checkpoint and artifact copies

**Checkpoint**: UUID, job/attempt/fence, global completed update, immutable manifest digest,
format/worker/runtime version, exact input digest set, world size/rank mapping, total bytes,
state `staging|replicating|committed|corrupt|purging|purged`, committed timestamp, optional promotion links.
Unique `(job,attempt,completed_update)`; re-upload of different bytes conflicts. `committed` only
after rank-complete manifest and two verified copies. Core stores no tensor bytes.

**RankStateManifest**: rank, adapter tensors/config, optimizer tensor tree plus primitive structure,
schedule definition/position, MLX current key, sampler generator state/cursor/permutation and progress
counters; safe relative file names, byte sizes, hashes, tensor keys/shapes/dtypes. No pickle.

**ArtifactCopy**: artifact UUID/kind/checkpoint-or-adapter FK, node ID, generated store key, manifest
digest, verification status/time, bytes and cleanup status. Exactly one current copy per artifact/node.
For a two-rank checkpoint, **each Studio stores the complete bundle of both ranks**, not just its
own rank's files. Checkpoint retention deletes only unreferenced artifacts after replacement commit.

**ArtifactTransferGrant**: opaque secret hash, source/destination declared node IDs, immutable
artifact/manifest digest, attempt/fence, allowed file set/bytes, expiry, revocation ID. Grant refresh
binds the same intent. The node stores only bounded renewable execution/transfer authority; core
decides whether a lost grant may be reissued. Specific grant revocation must not cancel unrelated
transfers. Source/destination paths are never supplied by callers.

Disk accounting reserves aggregate source/target staging and durable artifacts. Last complete
checkpoint cannot be evicted to make room for its replacement. Promotion copies serving artifacts
with its own retained reference; it does not permanently pin optimizer snapshots beyond quotas.

## 7. Adapter and independent verification

**Adapter**: UUID, model/base-variant IDs, globally unique stored public selector, slug, manifest,
parameterization/config, objective, source job/checkpoint and input/resolved-spec digests, metric
summary, runtime, state `validating|replicating|ready|failed|retired`, visibility default `admin_only`,
verification default false and evaluation identity, created/published/retired timestamps/version.
Adapter bytes/config are immutable. An updated adapter is a new identity, never an in-place revision.

Ready requires exact-base compatibility, successful inference smoke and both verified copies.
Publication requires ready adapter and a published ready base; effective entitlement requirements
are the union of base and adapter. Base retirement immediately refuses new pair selections. Explicit
unpublication prevents new ordinary-user selection; active requests follow existing revocation rules.

**HarnessEvaluation extension**: optional adapter ID and exact base manifest with existing suite,
engine/harness/runtime version, scorecard and verdict. A pass updates only matching pair verification;
failed reevaluation revokes that pair. Base verification is neither inherited nor overwritten.
Run tokens bind exact targets and require a live compatible pass for write-capable operations.

## 8. Events, metrics, coexistence and persistence

**TrainingEvent**: job ID + monotonic sequence, attempt/fence, kind, state version, safe typed payload,
timestamp. Sequence persisted in same transaction as state effect. Seven-day replay retention;
snapshot/reset cursor remains available. Read subscriptions cannot mutate or resubmit jobs.

**TrainingMetricSample**: job/attempt/completed update/kind unique key; train loss, validation loss,
learning rate, update/token rate, tokens, measured footprint/peak, recorded timestamp. Finite values
only; a nonfinite result becomes a typed failure event. Historical samples retain attempt identity
and a rolled-back segment marker on recovery. Paginate (<=2000) rather than returning unlimited curves.

**TrainingCoexistenceProfile**: UUID, immutable measurement report digest, node/runtime/hardware,
resident target multiset, train base/parameterization/optimizer/batch/sequence/world-size bounds,
baseline and mixed latency/throughput/swap/thermal measurements, validity/expiry, invalidation
reason. Only node-recorded measurement results can approve admission; admin cannot set pass=true.
Record frozen workload digest/concurrency/arrival schedule/token bounds, p95 metric/query version
and per-target completion counts. Approval needs >=100 completions per target in each 15-minute
baseline/mixed phase. Runtime latency eligibility requires >=30 first-token samples per target
in the trailing 5 minutes and telemetry age <=60 s; insufficient evidence is distinct from pass
or threshold breach. All resident target instances remain represented in the profile multiset.
Training memory-only profiles also record measured peak and conservative probe envelope. Measurement
jobs share admission, watchdog, audit and reserved memory rather than bypassing them.

One additive reversible migration adds these tables/nullable target columns and relevant constraints.
Check current Alembic head before assigning the planned `0031_sft_training.py` revision. Downgrade
must refuse live training, outstanding target references and uncertain artifact cleanup; it must
not silently drop recoverable work. Existing rows default to base-only semantics.
