# Research: Model Acquisition Pipeline

## R1. Durable workflow ownership

**Decision**: Pin `dbos==2.24.0` (MIT) in the scheduler dependency set. A deterministic workflow ID
derived from the acquisition UUID provides exactly one durable workflow. `@DBOS.workflow` owns the
stage sequence; retryable `@DBOS.step` functions issue idempotent node verbs and persist their
results. Recovery is started only by `coire-scheduler`.

**Rationale**: The constitution assigns durable workflows to the scheduler. DBOS persists step
outputs and recovers incomplete workflows after process restart, while the existing job UUID and
node idempotency prevent duplicate downloads or conversions.

**Alternatives considered**: Keep the polling reconciler (does not meet the explicit DBOS
requirement); run DBOS in the API (couples request restarts to orchestration); Celery/Temporal (new
service and operational surface without a concrete need).

## R2. Migration from feature 001

**Decision**: Keep `download_jobs` as a compatibility projection for one release and add
`acquisition_workflows`, `acquisition_stages`, `model_variants`, `variant_copies`,
`validation_results`, and `node_reservations`. Seed each existing model as one default variant and
retain its current on-disk path.

**Rationale**: Existing registry UUIDs, model files, audit rows, and active jobs survive the upgrade.
New APIs use variant rows; old CLI and `/v1` calls resolve through the default variant.

**Alternatives considered**: Rewrite existing tables in place (unsafe rollback and mixed-version
operation); move every model directory during migration (large, failure-prone data mutation).

## R3. Metadata-only inspection

**Decision**: Resolve the immutable Hub commit and inspect sibling metadata, `config.json`,
tokenizer configuration, safetensors indexes, and gating metadata through `huggingface_hub`. Do not
call `snapshot_download` or fetch weight blobs. Architecture support is checked against the pinned
mlx-lm model registry; `trust_remote_code` remains false.

**Rationale**: It provides architecture, format, exact LFS bytes, revision, and quantisation data
before transfer. It also prevents repository code execution.

**Alternatives considered**: HEAD every file (unnecessary requests); download then inspect (violates
FR-003); infer support from model names (fragile and violates capability-driven design).

## R4. Conversion invocation and recipes

**Decision**: coire-node launches the pinned CLI as an explicit argv:
`python -m mlx_lm convert --hf-path <raw-dir> --mlx-path <partial-dir> --quantize ...`.
Allowed modes are `affine`, `mxfp4`, `nvfp4`, and `mxfp8`; mixed recipes are `mixed_2_6`,
`mixed_3_4`, `mixed_3_6`, and `mixed_4_6` in mlx-lm 0.31.3. The contract is an enum/validated
combination and never exposes `--upload-repo` or
`--trust-remote-code`.

**Rationale**: This is the supported bare engine surface and preserves Principle I. An allowlisted
argv prevents shell injection and structurally makes Hub upload impossible.

**Alternatives considered**: Import private conversion functions (tighter version coupling); shell
command templates (injection risk); a conversion wrapper service (forbidden extra abstraction).

## R5. Memory and disk reservations

**Decision**: Inspection computes conservative peak memory from unquantised weight bytes plus a
configurable conversion overhead. The scheduler creates a durable node reservation before issuing
convert. If current committed memory prevents admission, the workflow remains `queued` with
occupants; if the unquantised peak exceeds total budget it is refused. Disk admission reserves raw,
partial output, and safety margin. Actual output updates estimate drift.

**Rationale**: Conversion can transiently require the unquantised model even when its result is
small. Reservations make the cost visible and prevent swap without prematurely implementing
feature 004 eviction.

**Alternatives considered**: Use output size alone (unsafe); immediately refuse a busy node (spec
requires queueing); evict models here (belongs to feature 004).

## R6. Validation

**Decision**: A node validation job runs three independently recorded checks: deterministic prompt
smoke generation with repetition/emptiness guards; token-average negative log likelihood over a
versioned held-out fixture; and tokenizer chat-template rendering of a canonical tool call followed
by JSON/schema validation. Perplexity is `not_comparable` without a reference. A template failure
fails validation; perplexity outside tolerance preserves files but marks the variant unvalidated.

**Rationale**: Results are reproducible, actionable, and sufficient to prevent publishing a broken
conversion without pretending that one metric proves general quality.

**Alternatives considered**: Load-only smoke test (misses degenerate output); external benchmark
suite (too expensive here; feature 017 owns broad evals); model-as-judge (not deterministic).

## R7. Raw retention and variant paths

**Decision**: Raw weights live under a private base-model raw directory keyed by immutable revision;
variants use registry-generated slugs. Raw deletion occurs only after validation and both variant
copies verify. A second variant reuses retained raw or explicitly dequantises a verified variant into
a new private raw workspace; it never performs an implicit Hub pull.

**Rationale**: Cleanup is crash-safe and publication can never point at partial output.

**Alternatives considered**: Delete immediately after local convert (cannot recover replica/validate
failure); place raw files beside served files (engine/path confusion).

## R8. Observability and security

**Decision**: Spans use `coire.scheduler.acquisition.<stage>` and
`coire.node.acquisition.<operation>`. Metrics cover duration, bytes, queued seconds, reservations,
validation outcomes, and estimate drift with model/variant/job/node identifiers in structured logs.
Alerts fire for stuck stages, conversion failure, and >10% size/reservation drift. Every admin
decision and stage transition appends an audit row.

**Rationale**: Long-running work must be attributable without logging secrets, prompts, or local
paths on public surfaces.

**Alternatives considered**: Logs alone (no duration/queue alerting); variant names as metric labels
(unbounded cardinality—UUIDs stay in traces/logs, metrics use stage/outcome/node).
