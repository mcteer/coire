# Implementation Plan: SFT Training Jobs

**Branch**: `feat/016-sft-training-jobs` | **Date**: 2026-10-03 | **Spec**: [spec.md](spec.md)
**Input**: `specs/016-sft-training-jobs/spec.md`; source baseline `6c1fccc` on feature 015.
**Status**: Implemented; supported runtime matrix and final release gates passed.

## Summary

Build administrator-managed datasets, reproducible SFT jobs, full-state checkpoint recovery and
registry-resolved adapters. Extend existing API/scheduler/node/web services. The scheduler owns
durable intent; native Studio processes call bare mlx-lm training APIs; core stores uploaded data
and metadata but no model/checkpoint weights. LoRA, QLoRA and DoRA, single-Studio and two-rank
data parallelism, chat-priority admission, CLI and console are all part of feature completion.

## Technical Context

**Language/Version**: Python 3.13; strict React/TypeScript in the existing Vite SPA.
**Primary Dependencies**: Existing FastAPI, Pydantic v2, SQLAlchemy 2, asyncpg, Alembic, DBOS,
httpx, PyYAML, OTel/Prometheus. Node lock currently contains `mlx-lm==0.31.3`, `mlx==0.32.2`;
retain the frozen wheelhouse/install workflow. No new dependency is planned. Declare PyYAML
directly in any runtime package that newly imports it, with its existing MIT licence and exact
lockfile pin; do not depend accidentally on mlx-lm's transitive dependency.
Implementation test amendment: declare the already-locked NumPy 2.5.2 (BSD-3-Clause) and
safetensors 0.8.0 (Apache-2.0) directly in the dev group for genuine synthetic CPU checkpoint
format tests on Linux/core. These are not production model/Metal workloads or new engine versions.
Declare the same existing safetensors 0.8.0 pin directly for coire-node's allocation-free
checkpoint-header validation; its Apache-2.0 licence and runtime purpose belong in the PR summary.
Documentation build amendment (2026-10-06): pin build-only `@types/node` **22.18.8**
(MIT) to type-check the Vite plugin that embeds the checked-in training runbook. No
new production runtime dependency or filesystem/network access is introduced.

**Storage**: Postgres 17; API-owned private dataset volume on core; node-owned datasets cache,
checkpoint and adapter stores on both Studios. Core never stores checkpoint/adapter tensors.
**Testing**: pytest unit/contract; real-Postgres concurrency and simulated-node integration;
Vitest/Testing Library and browser acceptance; offline <=1 GB single-Mac training fixture;
real-Studio recovery, all parameterizations, two-rank and coexistence matrix.
**Target Platform**: Existing hardened core containers; native macOS 26.2+ Studio workers.
**Project Type**: Distributed control-plane service, native workers, CLI and administrative SPA.
**Performance Goals**: Progress delivery <=2 s after persistence; healthy cancellation <=5 s;
pause checkpoint <=60 s or explicit forced-stop fallback; loaded chat <=1.5 s p95 first token
for <=4k prompts during a 15-minute measured training workload; existing <=20 ms p95 gateway
overhead remains a regression gate. No fixed tokens/s training claim across model sizes.
**Constraints**: Offline registry-only execution; full per-rank memory reservation; no silent
truncation; full-state rather than weights-only resume; no user dataset code; no model work on
core; pinned/active serving protected; training and image generation mutually exclusive per node.
**Scale/Scope**: Two declared Studios, one trainer per Studio; eight nonterminal queued/active
jobs globally and four per administrator; limits below. No new service or network permission.

## Constitution Check

Pre-research and post-design review against constitution **2.0.0: PASS for the design**.
Evidence gates in tasks/quickstart remain mandatory before implementation can be called complete.

| Principle | Design compliance and verification |
| --- | --- |
| I — Bare engines | Node-owned explicit-argv worker calls unchanged mlx-lm loader, adapter and trainer APIs. No inference wrapper, alternate trainer stack, loop fork or monkeypatch. Bare `mlx_lm.server` serves dedicated adapter instances. |
| II — Core/worker split | Core holds control/data metadata and uploaded JSONL only. Tokenization, training, inference and tensor artifacts stay on Studios. Mirrored artifacts plus core metadata avoid a Studio-only source of truth. Adapter engines are excluded from the existing text-only failover tier. |
| II-a — Containers | Extend existing distinct hardened images; no new runtime/service. Preserve non-root/read-only/capability/network/health restrictions and scan/SBOM gates. Native trainers use the existing node ownership exception. |
| III — Contracts | Strict shared models first; one reversible feature migration; generated OpenAPI/TS. Existing UUID model identifiers remain valid; adapter selector grammar is restricted and registry-resolved; Coire-compatible additions use `coire_`. |
| IV — Zero trust | Admin management, scoped short-lived node/artifact execution grants, browser origin enforcement, exact-target run tokens, fail-closed audit, cancellation and stale-attempt fencing. No arbitrary path, remote code or caller executable configuration. |
| V — Models as data | Ready local acquired bases only; derived adapters validated and mirrored before ready, admin-only/unverified initially, published explicitly, exact-pair harness gate. No implicit Hub downloads. |
| VI — Observable | Durable loss/events plus local spans/metrics/logs, Jobs/dashboard extension and baseline alerts. Disabled diagnostics storage never suppresses audit/progress/alerts. |
| VII — Spec/test gated | Explicit dependency-ordered tasks; contract tests for every boundary, real transaction races, offline tiny-model gate and measured Studio matrix. No skipped required gate counts as acceptance. |

No constitutional exception. Record an implementation ADR for bare trainer hooks, full checkpoint
commit, exact adapter targets and rendering identity. Update architecture §8.1 to reflect the
clarified uploaded-data scope and existing Studio-side rendering instead of promising a renderer
on core. These are design refinements, not permission to relocate inference.

## Project Structure

### Documentation (this feature)

`specs/016-sft-training-jobs/{spec.md,plan.md,research.md,data-model.md,quickstart.md,tasks.md,
checklists/requirements.md,contracts/training-api.md,contracts/node-training.md}`.
The subsequent Analyze report is read-only and returned in the session, not written as an artifact.

### Source Code (repository root; proposed additions are not existing capabilities)

| Concern | Paths |
| --- | --- |
| Shared types | `packages/coire-core/src/coire_core/models/{training,datasets,adapters,training_node}.py`; compatible changes to `{conversation,gateway,chat,engine,instance,harness,runs,mcp,auth,console,node}.py`; `errors.py`, `settings.py`, new `conversation_rendering.py` (serialization only) |
| Persistence | `apps/coire-api/src/coire_api/db.py`; `apps/coire-api/alembic/versions/0031_sft_training.py` after current head `0030_image_output_retention` (recheck before writing) |
| API | `apps/coire-api/src/coire_api/routes/{admin_training,admin_datasets,admin_adapters}.py`; `training/{service,specs,datasets,authorization,events,checkpoints,adapters,storage,retention,telemetry}.py`; `app.py`, `openapi.py`, `nodes_client.py`, new `training_executor.py` |
| Scheduler | `apps/coire-api/src/coire_scheduler/{training,training_admission,training_recovery,training_guard,datasets}.py`; existing `{main,workers,placement,instances,image_dispatch,sharded_instances}.py` |
| Serving/verification | `coire_api/gateway/{targets,resolution,loading,proxy,usage}.py`; `placement/service.py`, `nodes_prober.py`, `evaluations.py`, `routes/admin_evaluations.py`, `runs.py`, `run_tokens.py`, `run_executor.py`, `chat/service.py`, `failover/publication.py`; actual paths under `apps/coire-api/src/` |
| Node | `apps/coire-node/src/coire_node/training/{supervisor,journal,worker,objectives,datasets,rendering,sampler,loss,checkpoints,artifacts,guard}.py`; `routes/{training,training_artifacts}.py`; existing `{agent,engines,reservations,metrics,store,runs}.py` and `routes/{engines,failover}.py` |
| Agent/MCP | `apps/coire-agent/src/coire_agent/{__main__,gateway_model,harness,evals}.py`; `apps/coire-api/src/coire_mcp/tools.py`; exact target propagation only, still three MCP verbs |
| CLI/web | `apps/coire-api/src/coire_api/cli.py`; `apps/coire-web/src/pages/Training.tsx`, `api/training.ts`, `hooks/useTrainingJob.ts`, `components/training/`, `styles/training.css`; `App.tsx`, `components/AppShell.tsx`, `components/chat/ModelPicker.tsx`, `api/chat.ts` |
| Operations | `recipes/training/`; `deploy/compose/{compose.yaml,README.md}`; `deploy/observability/alerts/training.yaml`, `grafana/dashboards/jobs.json`; node wheelhouse packaging; `docs/runbooks/sft-training.md`, `docs/ARCHITECTURE.md`, `docs/design/DESIGN.md`, new ADR |

**Structure Decision**: Scheduler code resides inside `apps/coire-api/src/coire_scheduler/`
but executes in the separate scheduler image/process. Node `engines.py` is a file; use the
new `training/` package for helpers. Existing model/variant/instance UUIDs stay intact; training
job and attempt IDs are ULIDs, dataset/adapter/analysis/checkpoint IDs UUIDs. No broad ID migration.

## Phase 0 — Research decisions

[research.md](research.md) records the pinned-source and repository evidence. Three key constraints:

1. Stock `--resume-adapter-file` is weights-only; use the upstream `train()` injection hooks
   with a supplied optimizer, stateful batch iterator, explicit loss and callback.
2. Stock loading enables tokenizer remote code and can execute `config.model_file`; validate
   local manifests/config before safe direct loading. Offline flags alone are insufficient.
3. Current routing, engine deduplication, run tokens and scorecards mostly identify the parent
   model. Adapter identity must extend the whole request/lifecycle path, not just the picker.

No product clarification remains. Runtime compatibility probes are first implementation gates;
they do not justify silently downgrading full-state recovery, two-rank training or chat priority.

### Approved data-fabric prerequisite (2026-10-04)

The user approved an endpoint-preserving Studio data-fabric setup with rollback. Native pinned
MLX discovery identifies the direct interface; a deployment-managed helper snapshots the existing
bridge/address/netmask, moves the same declared replication address to that interface, and restores
the bridge on failure or explicit rollback. Native hostfile generation then validates this existing
setup rather than applying MLX's unrelated default /30. Device fields remain native-generated.
No control/fallback route, firewall, sudo-policy or RDMA privilege widening is introduced.
The initial operation is a runtime trial; persistent macOS network-service and reboot validation
remain mandatory before a durable deployment claim. Operator-authenticated root is required for
interface changes. Record application, rollback, authenticated link/transfer and persistence results
in the execution record; missing root credentials are a concrete prerequisite, not test success.

## Phase 1 — Design

### A. Contracts, immutable intent and authorization

`TrainingSpec` schema version 1 contains `model`, `data`, `objective=sft`, `parameterization`,
`optim`, `eval`, `output`, `placement`, and `seed`. Only registered immutable IDs are executable
inputs. Original YAML is capped, UTF-8, duplicate-key/tag/alias/depth-limited; the typed parser
rejects unknown keys and nonfinite numbers. Form submission generates YAML first. Preserve
original bytes/digest, canonical intent digest, and fully resolved spec/digest separately.
Retry matching uses canonical client intent before re-resolving defaults; same idempotency key
with changed intent is 409. Resolve and freeze selected base/variant/dataset/template identities,
runtime fingerprint and output-name reservation before queueing.

Use existing current-user/admin-scope guards, exact browser Origin policy, key rate limits and
transactional audit. Exclude ops/run/node credentials from admin management. Recheck authority at
dispatch, resume, checkpoint promotion and final publication; revocation requests stop and forbids
late publication. Record IDs, operation, safe reason and hashes, never source rows or raw trainer
stderr. Artifact routes use separate bounded node/attempt-specific grants, never admin tokens
inside trainers. Mutation audit failure rolls back the mutation.

### B. Datasets and consistent rendering

API streams uncompressed JSONL to a private quota-held staging file, incrementally schema-checks
rows and records a digest. Failed uploads never become ready. Source formats are `text`,
`prompt_completion`, and `conversation`; all normalize to `TrainingExample` plus canonical
`Conversation`, with an explicit `content_mode` and loss policy. Add optional tools, tool-call
IDs/results and bounded metadata compatibly; tool-only assistant turns may have empty text
parts only when valid tool calls exist. Preserve existing chat/image behavior.

Core may perform bounded JSON/schema/hash/split work; scheduler sends tokenizer-specific analysis
to a Studio CPU process with a local reservation. It loads tokenizer assets only, not base weights.
Upload supplies the registered analysis model/variant. Validation against a different training
variant creates a separate immutable analysis. Analysis failure is visible and retryable; no
training proceeds without matching successful preflight.

Text mode uses raw token encoding and all non-padding targets. Prompt/completion and conversation
mode default to the final assistant target; earlier assistant/tool turns are context. Validate
the final target and prefix alignment, reject zero supervised tokens and overlength examples;
no silent dropping/truncation. Conversation tools use JSON argument objects canonically and
OpenAI strings only at the necessary wire boundary. New shared core serialization performs no
tokenizer/model work. Studio analysis/training uses the same upstream
`process_message_content()` and `TokenizerWrapper.apply_chat_template()` primitives as bare
serving, with identical tools, template content, tokenizer and explicit thinking kwargs. Compare
completed-turn token sequences; `add_generation_prompt` is the documented serving suffix.
Correct the existing override path-vs-template-content mismatch as part of this binding and test
the actual server's rendered tokens. Never claim the server invokes a new Coire-only renderer.

Split canonical content hashes (excluding IDs/metadata) as groups using seed and immutable source;
95/5 train/validation default, both nonempty. Exact duplicates cannot cross partitions, including
across a mixture: conflicting source split memberships fail mixture preflight. Store row-index
manifests rather than copying merged corpora. `sample_count` limits each source pool;
`epoch_samples` and normalized proportions produce deterministic largest-remainder draw quotas.
Weighted strategy shuffles/interleaves quotas with explicit RNG; sequential strategy uses source
order. `replacement=false` must fit each pool, otherwise refuse. Sampler state includes next row,
permutation/epoch, mixture quotas, rank assignment and generator version. Keep independent
validation iterator/RNG. No runtime executable loader or Hub importer exists.

### C. Atomic admission, full reservations and serving protection

Implementation refinement (2026-10-06): macOS thermal observations may use the public
`NSProcessInfo.thermalState` API when the existing IOPMrootDomain field is unavailable.
Use explicit object/NSInteger native signatures, no privileged helper or new dependency.
Missing/invalid observations remain unknown and preserve the existing admission/guard refusal.
Validate actual readings from installed wheels on both Studios; never synthesize nominal state.

Native process-inventory refinement (2026-10-06): protected OS daemon argv is outside the bare
engine candidate set only when macOS kernel code-signing status proves both CS_VALID and
CS_PLATFORM_BINARY, executable metadata is readable, the executable is not a generic interpreter/
script host or Coire/MLX/mflux path, and process birth identity remains unchanged. Names/paths alone
cannot exempt a process. Inaccessible unsigned/interpreter/reused/unresolved processes still refuse
admission. This uses an unprivileged native status query, not a new service or privilege change.

Hardware identity uses the installed node package version that registration and health advertise,
not the shared control-plane SERVICE_VERSION setting. Compare the native fingerprint against the
core's canonical node-inventory fingerprint; configuration overrides must not change this identity.

Installed-runtime refinement (2026-10-06): retain the existing dedicated, independently identified
`coire-node-python` executable when creating frozen environments. With uv 0.12.7, an explicit path
inside `UV_PYTHON_INSTALL_DIR` can still canonicalize to the shared `python3.13` executable.
Provision under the declared prefix first, then omit managed-install/bin-directory overrides only
for the explicit `uv venv --python` invocation. Include the selected executable bytes in the
immutable environment identity and verify the final binding before smoke/publication or reuse.
Activation rejects a legacy/shared binding. This preserves the already-enabled runtime identity;
it does not change privacy settings, service user, network permissions or the shared interpreter.

Acquire existing per-node advisory locks in stable order for all affected nodes, then re-read
health, runtime/link evidence, counted reservations, active request leases, pins and training
slots. Data parallelism reserves **full weights + trainable parameters + optimizer moments +
activations + batch/communication/checkpoint buffers + safety margin on each node**. Include the
sandbox slice and failover reservation; no inherited sharded-inference half-weight estimate.
Preflight supplies measured/configuration-specific upper bounds, not caller estimates. Refuse
missing memory evidence; build evidence through bounded admin preflight on an otherwise safe
node with conservative hard headroom. Count that probe itself as training work.

The same transaction marks eviction victims draining/non-admissible, records restoration intent
and commands, inserts both pending holds and the fenced attempt. Dispatch only after commit;
start neither rank until every required eviction and node preparation is acknowledged. Pins and
new gateway leases must use the same locks. Initially exclude sharded inference groups from
automatic eviction rather than independently evict a rank; count them as protected occupancy.
Impossible fit is a terminal preflight refusal; live competing work is bounded queue waiting.

Training admission is bidirectional: model loads (single and sharded), image dispatch, conversions,
and gateway request leases must respect active training holds/guards. One trainer per node;
image generation and training are mutually exclusive. Chat may coexist only with a current,
exact measured profile covering node/hardware/runtime, multiset of resident **variant+adapter**
targets, train base/parameterization, world size and bounded batch/sequence/rank settings. No
adapter or duplicate-instance identity collapse. Profiles are immutable evidence from an
admin-triggered bounded measurement, not manually asserted pass flags; test baseline and mixed
15-minute runs before enabling a combination. Probe runs remain supervised and guard-protected.

Monitor rolling chat first-token p95, thermals, swap delta, physical footprint and stalled progress.
The latency guard evaluates each declared resident target separately every 5 seconds over a
trailing 5-minute window, requiring at least 30 first-token samples and telemetry no older than
60 seconds. Below the sample floor or beyond freshness, report insufficient evidence and block
new mixed admission; do not interpret missing traffic as healthy latency. An already running
attempt retains independent memory/swap/thermal/lease guards; insufficient latency samples alone
do not fabricate a breach. A measured p95 above 1.5 seconds with sufficient fresh samples is a
confirmed breach. Record the metric/query version so acceptance and runtime use the same p95
definition and cannot approve against different estimators.
On breach invalidate evidence, request checkpoint-pause immediately, and use node-local watchdog
termination if the 60 s pause deadline cannot be met; imminent memory danger can stop sooner.
Cancellation has an independent 5 s kill deadline. Node watchdog also enforces a renewable 30 s
execution lease on core loss. Recovering or unconfirmed processes retain counted `HELD` or
`RELEASING` reservations, never `FAILED` as a shortcut to free capacity. Training footprint must
join node health and ledger drift calculation, including physical-overage admission.

After confirmed death, release once and offer eligible recorded victims reload under current
capacity/version/pinning rules. Protective pauses may auto-resume after 60 s cooldown only if
fresh healthy evidence authorizes the exact combination; latency-invalidated evidence needs a
new measurement. Admin pauses require explicit resume.

### D. Bare training runtime and full checkpoint state

Node starts a native worker in its own process group with fixed module/explicit argv and
registry-generated local IDs. Record command digest, job/attempt/fence, reservation, PID/create
time and bounded control endpoint in the journal before acknowledging start. A durable spawn-intent
record plus unique process marker permits reconciliation across a crash between spawn and PID
recording; uncertain ownership never causes blind respawn. Re-adopt verified live workers.
Do not use acquisition `JobSupervisor.resume_all()`, which may respawn surviving work.

Safe load uses validated local manifest/config, rejects `model_file` and unapproved architecture/
tokenizer code, sets remote-code false/local-files-only/offline, strips Hub credentials and remote
reporting configuration. Preserve the bare engine libraries. Objective registry has only `sft`;
future objective names fail rather than silently storing inert configuration.

Use `linear_to_lora_layers()` and `mlx_lm.tuner.trainer.train()` directly. First supported matrix:
dense unquantized LoRA, affine 4-bit/group-64 QLoRA on an acquired quantized base, dense unquantized
DoRA with explicit linear targets. DoRA includes magnitude tensors; reject unsupported MoE,
quantized embedding/DoRA or target combinations before launch. Freeze base weights. Initially
support Adam/AdamW with constant or warmup-linear schedules; all optimizer fields are allowlisted.
Disable upstream class-mutating gradient checkpointing. This is a documented initial capability
bound, not an unimplemented accepted option.

For accumulation A and completed update offset u0, pass remaining microsteps `(updates-u0)*A`,
`steps_per_report=A`, independent custom batch iterator and masked loss. Keep upstream in-loop
validation off; callback invokes upstream `evaluate()` at completed-update schedule boundaries,
preserving training RNG. Training update numbers count optimizer updates, not microbatches.
The callback sees evaluated model/optimizer state with cleared accumulation. Require
`optimizer.step == u0 + local_iteration/A`; persist actual progress before presentation.

At every checkpoint, synchronize arrays; serialize exact trainable tensors and complete optimizer
tree (including moments, step and schedule values) into safetensors plus strict JSON structure.
Also save MLX RNG key, sampler RNG/cursor, immutable config/input/runtime digests, event counters
and rank identity. Reconstruct schedule functions from pinned settings. For MLX 0.32.2 restore
the saved current two-word RNG key through its equivalent 64-bit seed on the training thread,
then prove exact subsequent draws. Never merely reseed with the original run seed.
No pickle or arbitrary class deserialization. Verify keys/shapes/dtypes before restoring state.

Write a new checkpoint directory, fsync files and directories, publish immutable manifest by
atomic rename. Rank agreement happens at evaluated update boundaries; every rank contributes its
own full state. Mirror the entire common bundle to both Studios via scoped data-fabric grants;
independently verify every file. Only a fenced core transaction acknowledging all rank components
and both complete copies commits a durable checkpoint. Hold advancement while committing; bound
transfer waiting by the checkpoint/pause deadline. Incomplete bundles cannot replace the prior
recovery point. Dedicated artifact paths never impersonate HF repository manifests.

Pause is a coordinated callback unwind after commit; no accumulated-gradient recovery claim.
Give this unchanged upstream exception-propagation behavior an early compatibility test. Ignore
upstream scratch adapter saves for recovery/publication. A stopped attempt can resume only after
all old processes are dead/fenced and a matching complete checkpoint exists. Before the first
checkpoint, explicit step-zero restart is permitted and recorded. Corrupt newest manifests fall
back to the newest fully verified common checkpoint; absent valid state fails with a reason.

For two ranks, coire-node owns a separate training process group via existing declared-node
`mlx.launch`/JACCL facilities; no API-side SSH, new host discovery or control-fabric data fallback.
Initialize identical trainable parameters and compare digests before updates; persist rank-specific
state. Link/rank failure tears down both. World size, mapping and runtime remain pinned on resume.

### E. Adapter lifecycle, exact routing and verification

Finalization and explicit checkpoint promotion copy only serving adapter tensors/config into an
immutable adapter artifact, validate against the exact base manifest, smoke on a reserved Studio,
replicate over the data fabric, and atomically register readiness. Preserve checkpoint lineage;
promotion pins the immutable adapter artifact, not an unbounded set of optimizer checkpoints.
Finalization races cancellation under the job version lock. Cancel prevents automatic final output;
an admin may later explicitly promote a retained complete checkpoint with a new audit action.

Public selector is an existing model UUID or registry-issued `<model-uuid>@<adapter-slug>`.
Store the complete selector; never split arbitrary values into engine paths. Adapter row fixes
`base_variant_id` and artifact digest. Add a strict internal `InferenceTarget` carrying model,
variant and optional adapter UUID. Optional compatible `coire_variant_id` permits exact base
variant selection for harness evaluation/runs; an adapter selector must agree with its fixed base
variant. Admin/private checks still apply; IDs alone grant no authority.

Dedicated instance identity, load coalescing, node engine deduplication, readiness and reconciliation
use the exact target plus instance ID. Base-only queries explicitly exclude adapter instances;
adapter failures never fall back to base/smaller variant. Bare server receives local base and
`--adapter-path` only from node-resolved stores. Proxy strips/replaces engine model/adapter fields
using the resolved engine record; caller strings cannot trigger an in-process swap.

Carry target identity through native chat/picker, compatible chat/completions/messages and model
listing, engine/instance status, usage, evaluation requests/results, run manifests, node-to-agent
environment/transport and scoped tokens. Legacy model-only run grants authorize base-only work;
new exact-target grants cannot authorize another adapter. Every write path checks verification of
the exact pair at admission and execution. Existing harness suite records adapter-specific results
and can revoke a prior adapter pass; training/promotion/publication never sets verified. MCP retains
research/plan/apply, inheriting the target behavior through runs rather than gaining training tools.

Adapter failover is explicitly unavailable in 016. Exclude adapter engines from node failover
residency and base selection, reject adapter requests at the failover edge, and test that a resident
adapter sharing a base slug cannot answer a base request. Core-only training control remains
unavailable during failover; node lease expiry preserves the last checkpoint.

### F. Progress, UI, operations and compatibility

Job states: `queued -> preflighting -> reserving -> running -> finalizing -> succeeded`, with
`pausing -> paused`, `recovering`, `cancelling -> cancelled`, and `failed` exits. Attempts are
separate fenced executions; a failed attempt may recover within the same job. Persist ordered
events and immutable loss samples keyed by `(job,attempt,update,kind)`; recovery can rewind to the
committed checkpoint and the UI labels replayed/discarded attempt segments instead of flattening
them into a false uninterrupted curve. Prometheus aggregates are not the historical loss database.
SSE has `Last-Event-ID`, 15 s heartbeats and snapshot/reset after bounded retention.

The Training page uses existing shell/tokens and admin gating. Add recipe/form preview, immutable
source/resolved YAML views, split/mixture analysis, actual state/reason, loss/progress, checkpoint
promotion, pause/resume/stop, adapter publication and verification links. Feature 017 comparison
area says unavailable; `eval` supports held-out loss only. Use generated types and `useEventStream`;
no direct component `fetch`. Extend Jobs activity/kill rather than creating an unrelated dashboard.
CLI adds `data`, `train`, `adapter` groups in the installed argparse CLI, with explicit dispatch.

Spans: `coire.api.training.*`, `coire.scheduler.training.*`, `coire.node.training.*` plus dataset
and adapter operations. Metrics: `coire_training_` queue/updates/outcomes/checkpoint/recovery/
pause/cancel/memory/disk and `coire_dataset_` analysis; bounded node/state/reason labels, never
job/dataset/adapter/user IDs as metric labels. Correlation IDs stay in logs/spans. Baseline alerts
cover stalled progress, recovery/lease uncertainty, checkpoint replication, memory drift/swap and
coexistence protection. Dashboard links to persisted job history when diagnostics is disabled.

Schema migration is additive with nullable target fields/default base-only semantics. Do not
rewrite historical UUIDs or loss data. Gate feature exposure with `COIRE_TRAINING_ENABLED=false`
by default; enabling requires compatible nodes and completed gates. Before rollback disable new
work, stop/fence trainers, preserve artifacts, disable adapter publication, and refuse old nodes
commands requiring unknown target/checkpoint versions. Downgrade refuses live training/references
rather than silently destroying state. Back up metadata/dataset volume; mirrored Studio artifacts
require an explicit retention/backup runbook, not a claim that `pg_dump` contains tensors.

### Configured defaults and initial bounds

All settings live in `coire_core/settings.py` and are documented in compose README. Per-model
capabilities may tighten bounds, never enlarge them beyond operator limits.

| Bound | Initial value/policy |
| --- | --- |
| Recipe | 64 KiB UTF-8; max nesting 16; no YAML aliases/custom tags/duplicate keys |
| Dataset | 256 MiB/upload; 1,000,000 rows; 1 MiB/row; at most 100 displayed row diagnostics |
| Dataset storage | 20 GiB core total; 2 GiB free-space floor; staged failures purged within 24 h |
| Analysis | 1 GiB accounted CPU worker memory; 30-minute deadline; full rows, bounded streaming index |
| Mixture | <=16 sources; seed 0..2^32-1; proportions positive and sum within 1e-6 of one |
| Training | <=100,000 optimizer updates; sequence <=8192; global batch <=64; accumulation <=64; adapter rank <=128; supported profile may lower bounds |
| Target modules | Explicit built-in linear module allowlist per approved base; no regex/import expression |
| Queue/execution | 8 jobs globally, 4/admin; queue 24 h; cumulative active execution 72 h; one trainer/node |
| Checkpoint | Every 100 completed updates by default and at pause/final; retain latest 3, >=1 valid; <=20 GiB/job and <=200 GiB training/artifact store per Studio; 20 GiB free-space floor |
| Artifact size | Preflight holds full serialized state + one staging/mirror copy; refuse before launch if the job cap cannot fit two complete checkpoints |
| Lease/control | 30 s execution lease renewed every 10 s; cancel <=5 s healthy; pause/transfer commit <=60 s; local watchdog checks <=1 s |
| Recovery | At most 3 automatic execution attempts; fail visibly after safe teardown; failed/cancelled jobs are terminal and a new submission creates a new job |
| Evidence | Exact workload profile expires after 7 days or any identity/guard change; per-target 5-minute latency window, >=30 first-token samples, telemetry age <=60 s, monitor <=5 s; insufficient evidence blocks new mixed admission |
| Events/logs | Replay events 7 days; durable terminal snapshot/loss samples until admin job deletion; bounded 1 MiB sanitized diagnostic log/job; logs not raw data |
| Retention | Inputs pinned by nonterminal jobs; paused jobs remain pinned until explicit cancel/delete; no automatic artifact deletion of a live reference |
| Pagination | Default 25/max 100; bounded metric retrieval max 2,000 samples/page |

Numerical recovery gate: same hardware/runtime/world size; exact restored keys/shapes/dtypes,
optimizer steps, RNG and sample sequence; FP32 adapter/moment tensors `rtol=1e-5, atol=1e-6`,
loss `rtol=1e-4, atol=1e-5` over first and next 32 resumed updates. Test nonzero dropout,
accumulation and a changing schedule. These fixed acceptance thresholds are not a cross-hardware
bitwise reproducibility promise; do not loosen them solely to pass.

Coexistence acceptance uses a fixed recorded prompt-workload digest, request concurrency, arrival
schedule, requested output bound and tokenizer-specific input-length distribution covering up to
4,000 tokens. Freeze that workload before the baseline and reuse it unchanged for the mixed run.
Each 15-minute phase needs at least 100 completed requests **per declared resident target**;
aggregate counts cannot mask an under-sampled pinned or adapter target. A run missing that floor
is inconclusive and cannot approve a profile. Persist per-target latency samples/counts or their
bounded reproducible summaries and the p95 metric/query version with the report.

## Verification and implementation gates

1. Prove pinned bare runtime hooks, safe loader, rendering parity and full-state round-trip using
   the offline tiny fixture before broad service work. If a required hook is unavailable, amend
   the design explicitly rather than weaken resume semantics.
2. Build shared contracts and reversible persistence, then dataset/node/orchestrator/domain paths,
   exact serving targets, CLI and web. Contract/schema changes regenerate OpenAPI/TS together.
3. Exercise true Postgres admission/pin/lease and cancel/publication races, fencing, authorization,
   restart/re-adoption, corrupt artifacts and core outage with simulated nodes.
4. Run tiny-model training/inference and three interruption trials on an isolated single Mac;
   mirror the second store locally for CI (no real Studio targeting). Run the separate real-Studio
   matrix for both physical replicas, both ranks, all three parameterizations and chat priority.
5. Finish runbook/ADR/architecture/design/config updates, alerts/dashboard verification, image
   build/scan/SBOM and rollback evidence. Record results in `execution-record.md` during implementation.

## Complexity Tracking

No constitution violations or exceptions. Added complexity is limited to requirements with
concrete use: full recovery, two-rank execution, measured serving protection and exact adapter
identity. Preference training, visual training, generic plugin frameworks, adapter failover and
sharded adapter serving are deliberately outside the clarified scope.


### Acceptance amendment: legacy text completion compatibility

Live quickstart section 6 returned 404 for `/v1/completions`. Implement the
single-string prompt compatibility surface described in architecture §8 through
existing authenticated chat execution: map the prompt to one canonical user turn,
then translate text choices and SSE into OpenAI text-completion shapes. Preserve
exact registry/adapter selection, current credentials/run limits, cold placement,
usage accounting, cancellation and streaming finalization. Shared request/response
contracts live in coire-core; reject unsupported legacy batching/echo/logprobs
rather than silently invent behavior. Add route and fragmented-stream contracts,
regenerate OpenAPI/TypeScript, rebuild/scan affected images, and run live adapter
completion trials. This adds no engine wrapper or core tokenizer/model work.


### Acceptance amendment: initialize native worker telemetry

Native worker execute spans were absent in actual Tempo queries despite parent
start/ingest spans. Both worker subprocess entry points must initialize the existing
node OTLP providers. Their supervisor passes the configured nonsecret OTLP endpoint
explicitly in its minimal offline environment; it never inherits credentials or
arbitrary environment variables. Test actual SDK span/counter export in an isolated
Python process and the supervisor environment boundary. Stage frozen node wheels,
then activate only after in-flight measurements stop. Runtime-bound profiles must
be measured again after activation; previous evidence remains retained separately.

### Acceptance correction: abandoned checkpoint alarms
Live cancellation leaves retained, incomplete checkpoint lineage for cleanup. Replication-overdue baselines must stop alarming on that lineage only when its job is terminal or fence superseded and every old rank has immutable stop proof. Current checkpoint transfers and old ownership without complete proof remain actionable. This read-only reducer correction preserves artifacts and does not free reservations or mark a partial checkpoint ready; verify database regressions and actual alert clearing.

### Native CPU analysis exporter follow-through
The analysis child has the same provider-startup gap as the numerical workers. Forward only its configured nonsecret OTLP endpoint and initialize the existing SDK after owned envelope validation, before guarded CPU tokenizer work. Regression must fail before the fix, and actual analysis trace export must pass. Stage and verify immutable environments; activate only quiescent nodes. The A-only analysis change may be validated while B runs an isolated measurement, with byte identity of all numerical worker/core modules recorded; B stays on its measured environment until acceptance ends.

### Acceptance correction: single-node resident health

Live coexistence admission exposed use of the sharded-only `rank_healthy` field for single-node instances. Single-node placement records liveness on `EngineProcessRow`. Validate READY state plus instance, node, port, model, variant and adapter identity on that owned row; retain verified manifest checks, node health and reservation admission. No health flags or measured observations are fabricated. Real Postgres coverage includes the normal false sharded flag and rejection of stopped or mismatched engines.

Node dispatch additionally freezes the logical instance-to-engine map in `TrainingMeasurementPrepare.resident_engine_ids`. Node inventory verifies engine IDs while gateway lease snapshots retain logical instance IDs; the authenticated gateway refuses replacement engines. This additive internal contract is deployed together with node and scheduler builds. Interrupted experiments remain inconclusive and require a fresh audited submission.

Worker trace attribution follow-through: enclose ordinary native execution, including load/render/checkpoint stages, in one `coire.node.training.worker` span bound to job, attempt and node. Measurement workers already have their corresponding owned parent. This telemetry-only change leaves numerical training functions and pinned dependencies unchanged; defer activation on occupied nodes until experiments are terminal.

### Acceptance correction: measurement request-loop responsiveness

The native measurement watchdog performs synchronous SQLite and process observations
inside the ASGI event loop, and lease renewal returns a synchronous status read.
Move these blocking operations and the durable lease write to the existing thread
executor, as the ordinary training observer already does. Retain the async command
lock through write completion, including request cancellation. Keep observations sequential and preserve exact
deadline, ownership and stop-proof rules. A bounded occupied-journal regression
must fail before the correction and show the request loop remains responsive after
it. Gate and package the immutable runtime; defer activation until current native
measurements are terminal. This defect is independently demonstrated; attribution
of observed live latency spikes still requires a controlled experiment.

### Acceptance correction: failed workload diagnostics

Retain the strict frozen gateway workload and its admission gates. Emit bounded failure reasons in logs, tracing and a counter so actual occupied slots and completion deadlines can be distinguished from routing identity or operation failures. Never emit prompt contents, credential values or exception messages. Cover blocked generation with a CPU unit test; only full real Studio windows can approve profiles. This implements Constitution VI without changing wire contracts or trust boundaries.

### Acceptance correction: training-only drain ownership

A live idle-eviction trial produced a training-drain decision that ordinary placement interpreted as a load policy. The controller also requested native status before an unsent preparation existed. Keep unload decisions on their existing command reconciler and drain workflow, exclude them from load dispatch, and defer native observation only while the current attempt is preparing and its job reserving with no preparation or an unsent preparation. Preserve reservation and uncertain-dispatch rules. Add real Postgres regressions before deployment and repeat the affected hardware trial.

### Acceptance correction: retiring upstream idle connections

The diagnostics experiment has actual pre-header RemoteProtocolError failures (server disconnected without sending a response), distinct from latency and occupied concurrency. Bound the gateway pool idle reuse to one second, below the node server’s five-second idle keepalive, retaining pooled reuse for immediate calls and the existing connection-count bounds. Prove stale-peer replacement with a TCP regression and repeat the same frozen five-second-arrival Studio trial. No retries, stream replay, wire changes or acceptance-gate relaxation are permitted. The live causal conclusion remains pending.

The live drain failure also exposed cancellation before preparation. Persist an existing typed TrainingStopRequest as a private fsynced no-start tombstone only after the journal/artifact/process inventory is pristine. Replay only the same immutable job/attempt/fence/node/rank/request scope, acknowledge stopped with no PID, and reject every later prepare/start for that attempt. Do not acknowledge absence if native ownership is uncertain. This needs no new wire shape; both payload models already reside in coire-core.

### Acceptance correction: shared ownership lock at the proxy

The quiet 5000-ms baseline reached the rolling latency limit after twelve minutes; whole-window averages cannot override that failure. Engine proxy lookup synchronously acquires the shared native memory lock, and the ordinary watchdog still reads its journal on the ASGI loop. Offload those blocking calls while retaining lock scope and sequential observation. Add stage spans around owned engine lookup, adapter resolution and bare upstream streaming; no prompt/credentials are emitted. A bounded occupied-lock heartbeat regression must distinguish the correction from merely loosening a latency gate. Actual latency attribution and a passing full profile remain required.

T142 bounds optional Tempo at 1 GiB with a 768 MiB Go runtime budget after actual OOM restarts under 512 MiB. This keeps diagnostic storage private and bounded and preserves existing traces; no engine, network, credential or acceptance limits change. Constitution VI requires usable diagnostics under acceptance load.

T141 also moves periodic orphan inventory outside the engine ownership lock and executes reconciliation in a worker thread. Discovery rechecks concurrent PID ownership and exact process birth/liveness before insertion; existing exact target reconciliation, drift/orphan reporting and persistence stay protected. Standard authenticated node request trace propagation connects native stages to their gateway parent.

T143 uses the public Darwin POSIX_SPAWN_CLOEXEC_DEFAULT flag for native read-only OS probes. Actual process sampling shows gRPC PrepareFork waiting while holding the Python GIL. The replacement keeps exact argv and deadlines, private bounded output and closes every descriptor except explicit stdio; it neither disables telemetry nor loosens workload gates. Constitution IV and VI apply.
