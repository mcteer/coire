# Research: SFT Training Jobs

**Date**: 2026-10-03 | **Baseline**: `6c1fccc` | **Method**: repository and pinned upstream
source inspection. No training/runtime/hardware probe was executed during planning.

## R1 — Use bare trainer APIs; stock resume is not full resume

**Decision**: Node-owned Python worker invokes unchanged `mlx_lm.tuner.trainer.train()` with
supplied optimizer, custom loss, stateful `iterate_batches` and `TrainingCallback`. Build only
the lifecycle/dataset/checkpoint integration; do not fork the trainer or add a wrapper platform.

**Evidence**: [mlx-lm 0.31.3 lora.py](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/mlx_lm/lora.py)
loads `resume_adapter_file` with `model.load_weights()` and then creates a new optimizer.
[trainer.py](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/mlx_lm/tuner/trainer.py) accepts all
four injection points, evaluates state before reporting, clears accumulated gradients on update,
and otherwise saves only trainable adapter tensors. It counts microbatches, not optimizer updates.

**Rationale**: Restoring only weights violates FR-014/015. Callback at each completed update
exposes evaluated model/optimizer state with no outstanding gradient accumulation. A coordinated
worker exception unwinds after a pause checkpoint; first-task tests prove that compatibility.
Disable upstream validation scheduling and invoke `evaluate()` at explicit global updates. Keep
its normal adapter output as scratch only. Disable `grad_checkpoint`: upstream mutates the layer
class `__call__`; no monkeypatch is required by this feature.

**Alternatives rejected**: Shelling out to stock resume loses optimizer/sample state; a training-loop
fork duplicates numerical logic; third-party wrappers violate the chosen bare-engine boundary.

## R2 — Restore exact optimizer/RNG/data state

**Decision**: Safetensors plus strict JSON manifests, per-rank optimizer and sampler trees,
completed-update numbering, immutable schedule declaration and validated full state keys.

**Evidence**: [MLX 0.32.2 optimizers](https://github.com/ml-explore/mlx/blob/v0.32.2/python/mlx/optimizers/optimizers.py)
supports `optimizer.state` assignment but permits subsequent initialization; missing moments
must be rejected before this can silently happen. Schedule callables need reconstruction.
[RNG bindings](https://github.com/ml-explore/mlx/blob/v0.32.2/python/src/random.cpp) expose a
thread-local `random.state` sentinel without indexed assignment. [Key construction](https://github.com/ml-explore/mlx/blob/v0.32.2/mlx/random.cpp)
maps a 64-bit seed to two uint32 key words. Restore the saved *current* key through that equivalent
seed and verify next draws, rather than resetting the original experiment seed.

**Rationale**: Stateful deterministic iterator avoids upstream global NumPy shuffles, implicit
batch dropping and sequence truncation. No prefetch beyond the committed cursor. Validation has
independent RNG. Checkpoint only after complete accumulation windows; partial gradients stay
internal to upstream `train()` and are not safely recoverable.

**Alternatives rejected**: Pickle is executable; seed-only reconstruction loses position; claiming
numerical similarity without exact optimizer/RNG/sampler restoration would conceal broken resume.

## R3 — Common two-rank checkpoint is a commit protocol

**Decision**: At the same evaluated update, both ranks stage full state; immutable common manifest
is mirrored and independently verified on both Studios; core commits its ID under current fence.

**Evidence**: Upstream trainer averages gradients with [MLX nn utilities](https://github.com/ml-explore/mlx/blob/v0.32.2/python/mlx/nn/utils.py)
but does not broadcast initial adapter parameters. Initialize and verify matching parameters.
Collectives coordinate state but provide no durable commit, timeout or process fencing.

**Rationale**: Per-rank RNG/sampler state differs. Rank/link loss can block a collective, so node
supervision kills both and holds uncertain reservations. Never independently choose each rank's
latest file. Resume world size/rank mapping/runtime is fixed. Mirror checkpoint tensors on Studios,
not core. Directory fsync and atomic manifest publication are required beyond existing file rename.

**Alternatives rejected**: Rank-zero-only state cannot restore rank RNG; local-only success leaves
a Studio sole source of truth; treating collective acknowledgement as a storage commit loses data.

## R4 — Safe local loading requires more than offline flags

**Decision**: Validate exact local manifests/config, reject executable model selectors, then use
`load_model(..., strict=True)` and tokenizer loading with remote-code false/local-files-only.

**Evidence**: `lora.run()` requests `trust_remote_code=True`.
[utils.py](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/mlx_lm/utils.py) can execute a local
`config.model_file` with `exec_module()` independently of tokenizer trust. Runtime defaults also
include remote dataset names/reporting destinations not appropriate for Coire.

**Rationale**: Admin acquisition authorizes data, not code execution. Reject escaping symlinks,
custom-code tokenizer requirements and arbitrary class/module names; remove Hub credentials and
remote reporting. Missing local files fail rather than fetch. Analysis tokenizer work stays on
Studio CPU; core never imports model/tokenizer runtime for this feature.

**Alternatives rejected**: Passing an arbitrary upstream config or relying solely on
`HF_HUB_OFFLINE` does not constrain local executable code.

## R5 — Rendering parity is Studio-side and tool-aware

**Decision**: Shared core canonical serialization; analysis/training reuse upstream serving
`process_message_content()` and `TokenizerWrapper.apply_chat_template()` on Studios. Pin
tokenizer, template content/digest, tools and explicit thinking kwargs. Use custom target masks.

**Evidence**: Existing `coire_core/models/conversation.py` lacks tools and forbids empty parts.
Gateway `gateway/context.py` explicitly avoids tokenizers on core. Upstream
[server.py](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/mlx_lm/server.py) joins multipart text,
normalizes empty content and parses tool argument strings; [datasets.py](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/mlx_lm/tuner/datasets.py)
does not apply all those steps. [Tokenizer wrapper](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/mlx_lm/tokenizer_utils.py)
forces `return_dict=False` on the HF path, so assistant-mask dictionaries cannot be assumed.
The upstream default loss uses a contiguous interval; custom masks must exclude the padded target
boundary explicitly. `mask_prompt` is final-message masking, not every assistant turn.

**Repository mismatch**: `coire_node/engines.py` writes a template file and passes its path to
`--chat-template`; pinned server treats that argument as template **content**. Feature work must
bind and test the actual template content consistently for base and adapter serving.

**Rationale**: The draft promise of a renderer in the gateway did not match the implementation.
Same upstream primitives plus token-level serving fixtures give a concrete shared implementation
without moving tokenization to core or inventing a server hook. Plain-text datasets explicitly use
raw encoding; conversation masks initially target the final assistant and reject prefix mismatch.

**Alternatives rejected**: A parallel Jinja renderer drifts; stock ChatDataset alone differs from
serving; silently dropping tools or images corrupts data. Images are explicitly outside training.

## R6 — Approved LoRA/QLoRA/DoRA matrix

**Decision**: Explicit linear targets; dense unquantized LoRA/DoRA and acquired affine 4-bit/g64
QLoRA first. All three require real training, recovery and serving tests. Unsupported matrices fail.

**Evidence**: [tuner/utils.py](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/mlx_lm/tuner/utils.py)
provides `linear_to_lora_layers(..., use_dora=...)`. QLoRA is LoRA on already quantized weights,
not another upstream fine-tune-type. [DoRA](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/mlx_lm/tuner/dora.py)
includes magnitude tensors, rejects Switch/MoE targets and can dequantize/reconstruct dense weights
in forward. It must not inherit QLoRA memory estimates. Quantized embedding behavior is not a
safe initial supported combination.

**Alternatives rejected**: Universal model claims without tests; runtime quantization hidden in
training; `full`/DPO/ORPO as accepted-but-inert recipe choices.

## R7 — Reuse locks and ledger, not sharded-inference memory semantics

**Decision**: Existing `placement/service.py` ordered PostgreSQL node-admission locks protect a
single atomic decision for both ranks, victims, holds and attempt fencing. Full training memory
on each node; one trainer slot; image/training mutual exclusion. Treat existing sharded inference
as protected occupancy rather than adding group eviction in this feature.

**Evidence**: `coire_core/models/placement.py` already has `ReservationHolder.TRAINING`.
`coire_scheduler/sharded_instances.py` divides weights by two, inappropriate for data parallelism.
`coire_scheduler/placement.py` has separated victim marking; training must mark victims draining
within the initial transaction. Model pin changes need the same locks. Existing failure handling
can mark reservations FAILED after stop errors; that state is not counted. Keep uncertain training
holds counted until node process proof. `nodes_prober.py` and physical-overage logic currently
account engines/images only and must include trainers.

**Alternatives rejected**: Per-rank independent reservation risks partial starts; treating a failed
job as proof of released RAM risks swap; eviction by node without group awareness kills one shard.

## R8 — Measured coexistence and node-local enforcement

**Decision**: Training-specific immutable measured profiles and guard; reuse image design patterns,
not image evidence. Exact target multiset and training bounds; pinned operations model included.

**Evidence**: `coire_scheduler/{image_admission,image_dispatch,image_latency}.py` provide expiry,
fingerprints, fail-closed admission and latency invalidation. Their variant-ID set loses adapter
identity and multiplicity, and their response is image cancellation rather than training pause.
Gateway request leases exist, but failover requests bypass core leases. Both forward/reverse
model admission paths plus node-local lease/watchdog enforcement are needed.

**Alternatives rejected**: “Fits memory” does not prove latency; copying image profile approval
would authorize an unmeasured workload; a scheduler-only kill path cannot work during core loss.

## R9 — Process journals and artifact grants

**Decision**: Dedicated training supervisor/journal with exact command hashes, process create time,
attempt fencing and stop proof. Scoped immutable artifact grants use Studio data fabric only.

**Evidence**: `coire_node/jobs.py::resume_all()` respawns jobs absent from its in-memory map, so it
can duplicate a surviving trainer. Better patterns are `engines.py`, `image_jobs.py` and
`image_runtime/supervisor.py`. Existing transfer flow is
`registry/acquisition_executor.py -> nodes_client.py -> node grants/routes/export/worker.py`;
`coire_core/net.py::DataFabricClient` rejects control-fabric fallback. Current grants bind slugs,
are volatile, and revoke by slug: checkpoints need independent immutable manifest/recipient/
attempt binding, specific revocation and safe grant renewal. Node local reservation is secondary
safety, not authoritative ledger accounting; disk holds need aggregate accounting.

**Alternatives rejected**: Acquisition restart semantics; arbitrary mutable training directory
export; core image-blob transfer for tensors; a node journal as the system of record.

## R10 — Adapter identity is an end-to-end change

**Decision**: Strict public UUID-or-pair selector and typed internal exact target, dedicated
instance and exact-target verification/token scope. Base-only queries exclude adapters. Feature
016 refuses adapter failover and sharded adapter inference explicitly.

**Evidence**: `gateway/{resolution,loading}.py` resolve/coalesce by model UUID; scheduler
`{instances,placement}.py` can adopt engines by model/node; node `engines.py` deduplicates by slug.
`evaluations.py` verifies `ModelVariantRow`, while CLI evaluation and agent transport can request
the parent model rather than the verified variant. `runs.py`, `run_tokens.py`, `run_executor.py`,
native chat picker and usage need exact target propagation. Failover matches resident engines
to snapshot models by base slug; merely omitting adapter snapshot entries is insufficient.

**Rationale**: Otherwise a verified base could authorize an unverified adapter, the wrong variant
could be scored, or a base request could accidentally receive an adapter. Add a compatible
`coire_variant_id` for exact base evaluations; pair selectors pin their own variant. Token model
scope alone is never sufficient for an adapter.

**Alternatives rejected**: UI-only aliases; passing adapter strings to the engine; overwriting base
capabilities; silently exposing adapters in failover without exact signed snapshot support.

## R11 — Dataset, privacy and retention decisions

**Decision**: Uploaded JSONL only (user clarification); private core source store and Studio
tokenizer analysis; immutable revision/digest/split and bounded diagnostics. Exact duplicates only.
Deterministic index-based mixtures; no merged dataset. Paused inputs are pinned; terminal purge
keeps provenance while marking reproduction unavailable. Promoted adapters retain their own
artifact, allowing optimizer checkpoints to stay bounded.

**Rationale**: Satisfies 016 independently of future feedback/consent or Hub acquisition workflows.
Loss history belongs in Postgres, not optional Prometheus/Loki history. Limits and failure modes
are in plan/contracts, not delegated to an unlimited upstream loader.

**Alternatives rejected**: Streaming remote datasets, executable loaders, implicit chat-history
training, silently sampling away invalid rows, or dropping source identity after deletion.

## R12 — Existing tools and verification gates

**Decision**: Use installed argparse CLI at `coire_api/cli.py`; add explicit group dispatch.
Use `uv run python -m coire_api.openapi [--check]`, not the older nonexistent export command.
Generate TS via `openapi-typescript`; use existing web testing/lint/build. New training engine
fixture consumes offline `COIRE_TEST_MODEL` <=1 GB and a local mirror store.

**Evidence**: Existing real text-engine tests download their own model and do not consume that
environment variable; they are not automatically the new offline training gate. Existing atomic
lock tests mock sessions; new transaction races must run against Postgres. Source migration head
is `0030_image_output_retention`; recheck when implementing.

**Alternatives rejected**: Marking skipped engine tests as acceptance, assuming mock ordering proves
transaction safety, or targeting real Studios from CI. Agent-run Studio tests are authorized by
AGENTS.md and are mandatory manual/agent acceptance, separate from CI.

## R13 — Explicit measurement evidence floors

**Decision**: Following the read-only analysis finding U1, the user approved an explicit evidence
protocol on 2026-10-03: identical frozen workload for 15-minute baseline/mixed phases, >=100
completed requests per declared resident target per phase; live per-target latency uses a trailing
5-minute window, >=30 first-token samples and <=60-second telemetry freshness, checked every
5 seconds. Insufficient evidence blocks new mixed admission; a confirmed threshold breach pauses
training. Other protection remains independent of latency sample count.

**Rationale**: A shared p95 limit without a window or minimum sample floor would allow incompatible
approval decisions. Workload and metric/query identities are recorded, so baseline, acceptance
and runtime comparisons cannot silently change their load or estimator. Under-sampled measurements
are inconclusive, not passes. The plan, data model, API contract, quickstart and T096/T099/T101/T111
now carry the same approved rules.

**Alternatives rejected**: Aggregate sample counts masking an idle pinned target; accepting an
under-sampled run; treating missing telemetry as evidence that chat is healthy.

## Acceptance probes still to execute

Pinned API/loader/rendering compatibility; state round-trip including nonzero dropout and negative
reset controls; three interrupted trajectory comparisons; two-rank partial-write/link-loss/stale
fence matrix; true concurrent admission/pin/lease/publication transactions; 5/60-second control
deadlines; all three parameterizations served from mirrored artifacts; 15-minute chat/training
progress with no swap growth; baseline alerts, hardened image scans and rollback. Planning has
resolved the implementation approach, not supplied these future measured results.
