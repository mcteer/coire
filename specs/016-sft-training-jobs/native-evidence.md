# Feature 016 native node integration evidence

## Implemented boundary

`coire_node.agent.create_app(..., training: TrainingSupervisor | None = None)` registers
the following bearer-authenticated routes only on `NetworkPath.CONTROL`:

| Route under `/node/training` | Input | Response |
| --- | --- | --- |
| `POST /attempts/{id}/prepare` | `TrainingPrepareRequest` | `TrainingPrepared` |
| `POST /attempts/{id}/inputs` (202) | `TrainingInputsRequest` | `TrainingPrepared` |
| `POST /attempts/{id}/start` | `TrainingStartRequest` | `TrainingStartReceipt` |
| `GET /attempts/{id}` | validated training ULID | `NodeTrainingStatus` |
| `GET /attempts/{id}/events?after=N` | nonnegative cursor | `NodeTrainingEventPage` |
| `POST /attempts/{id}/lease` | `TrainingLeaseRenewal` | `NodeTrainingStatus` |
| `POST /attempts/{id}/pause` | `TrainingPauseRequest` | `NodeTrainingStatus` |
| `POST /attempts/{id}/stop` | `TrainingStopRequest` | `TrainingStopReceipt` |
| `POST /attempts/{id}/checkpoint-commit` | `CheckpointCommitAcknowledgement` | `NodeTrainingStatus` |
| `POST /reconcile` | `TrainingReconcileRequest` | `TrainingReconcileResult` |

Every scoped mutation rejects a different path attempt or addressed Studio before dispatch.
Supervisor/journal fencing, lease, command identity and immutable source verification remain
the authority. Refusals return a bounded 409 message without exception content. Disabling
training refuses prepare/input/start/lease/new analyses, while status/events/reconcile/pause/
stop/checkpoint acknowledgements remain available for existing owned work.

`submit_inputs(command, *, settings) -> TrainingPrepared` schedules owned asynchronous grant
delivery, returns `ready=false, reason=analysis_pending` while staging/validation is active,
and never persists grant secrets. Poll prepare using its immutable command to obtain actual
readiness; input acceptance alone is not readiness. Sources must cover exactly the resolved
source set. Downloads use the declared core origin, node bearer and scoped grant headers,
no redirects or environment proxies, exact byte/hash limits and private atomic source staging.

Single-source readiness verifies staged binding/split/analysis/source identity, acquired base
manifest/configuration and local tokenizer/template/runtime digests. Start repeats validation.
Native validation threads and source transfer tasks remain owned across observer disconnects;
stop waits for staging termination proof and cannot race a late worker spawn.

## Production ownership/accounting

On modern-network declared Studios, `serve` creates the native journal/supervisor even when
`training_enabled=false`. Its interpreter is the configured node runtime's `sys.executable`;
production node installation/launchd supplies the versioned runtime. Worker argv is fixed,
and native children inherit only the minimal offline environment, with no Hub/DB credentials.

The journal uses the same `threading.RLock` as engines, image workers and reservations.
Admission availability callbacks are evaluated under that lock, rather than at a route's
earlier observation time. Engines and conversion reservations count native training memory;
training counts engines/image/reservation holds. Unknown owners retain the full envelope;
measured native physical-footprint overages increase its counted commitment. Image loading
and training admission mutually exclude each other. Filesystem-scoped disk callbacks count
retained training state and source/index holds in conversion admission, and conversion holds
in training admission. Source holds are acquired before download/copy and re-evaluated under
the same lock. The configured artifact quota bounds the available disk envelope.

Before listeners bind, native status observes existing argv/PID/create-time ownership without
respawning. Metrics attachment includes native status and memory occupancy and refreshes the
first production health sample. The native watchdog runs independently of the enable flag,
observes ownership, enforces leases/pause deadlines and releases memory only after death.
Stop routes release after a matching positive stop receipt. Retained disk remains counted.
Shutdown drains/cancels private preparation tasks before closing the journal, leaving native
workers available for re-adoption. Safe control/preparation/stop logs, OTel spans and bounded
outcome counters are emitted without source/grant content.

## Verification

- Ruff check and format passed for the six changed node source modules and lifecycle tests.
- Strict mypy passed for `agent.py`, `metrics.py`, `routes/training.py` and
  `training/{journal,supervisor,worker}.py` (6 source files).
- Focused CPU gate: **76 passed** for lifecycle, analysis routes, reservation accounting,
  worker control and checkpoint store tests; no skipped tests in this gate.
- Broader node CPU unit/contract run: **619 passed, 1 platform-conditional skip**. The skip
  is the pre-existing non-Darwin physical-footprint fallback test on this Darwin host;
  it is not training acceptance. Final minor preparation-metric changes were verified by
  the focused gate and Ruff/mypy after that broad run.
- New contracts exercise auth/control/data separation, wrong Studio scope, disabled-history/
  cancellation availability, asynchronous pending receipts, credential-free journaling,
  both directions of shared training/conversion admission and local lease watchdog release.
- Existing tests retain single-source binding/rendering and cancellation/thread/spawn races,
  fenced replay, half-spawn/PID reuse recovery, offline argv and checkpoint invariants.

No commits, network changes, downloads, Studio production edits or hardware workloads were
performed. No Metal work ran on core. Numerical tiny-model and physical-cluster acceptance
are not claimed by this CPU evidence.

## Concrete remaining contract/integration dependency for the main agent

**Mixture execution is not complete.** At this evidence point, shared `training_node.py`
still has `CheckpointWorkerState.sampler: SingleSourceSamplerState` only. The delivered
`coire_node.training.sampler.MixtureSampler` returns an internal dataclass, not a core wire
contract. Do not remove the explicit fail-closed mixture execution gate until its full-state
checkpoint can be validated and restored.

Required shared interface, matching the delivered sampler:

- `MixtureSamplerState`: immutable `identity_sha256`, nonnegative bounded `epoch`,
  next-unconsumed global `cursor`, `rank`, `world_size`, and fixed
  `generator_version="sha256-counter-v1"`, with a discriminator if using a union.
- `CheckpointWorkerState.sampler`: strict discriminated single-source/mixture union.
- Sampler `snapshot()` returns that shared model; `restore()` accepts and validates it.

Then wire `worker` compilation across `load_all_frozen_inputs(...)`: index each independent
train pool using `index_training_source`, construct `MixtureSampler` with resolved quotas/
strategy/replacement/seed/digest, build independent held-out evaluation, extend checkpoint
restore typing, and test next-batch equivalence after full-state restore. Preserve the current
single-source compiler/sampler for its numerical compatibility. Multi-source grant staging,
private per-dataset files, exact successful analyses and counted aggregate holds already exist.

Two-rank start also continues to fail closed: the existing native worker lacks common-rank
full-state checkpoint coordination and the supervisor deliberately rejects `world_size != 1`.
This is not advertised as data-parallel readiness. Extraction remains the parallel owner's
surface; its production manager registration should be coordinated with `agent.py`.

Feature 016 cannot be marked complete from this evidence alone.

## Continuation: production measurement/extraction ownership wiring

The following records the subsequent agent/metrics/reservation integration. It supersedes
the earlier extraction-registration dependency above. Shared mixture contracts and native
distributed worker code have since been delivered by their respective owners; this continuation
does not edit those workers or assert their hardware acceptance.

### Production constructors and hooks

```python
create_app(
    ...,
    training_measurements: MeasurementSupervisor | None = None,
    training_adapter_extractor: AdapterExtractor | None = None,
)

await serve(
    settings, collector,
    measurement_active_leases: Callable[[set[UUID]], int] | None = None,
)
```

`serve` now builds `MeasurementSupervisor` with a separate private journal under
`training/measurements`, the SAME admission RLock as engines/reservations/trainers,
the installed node interpreter, generated private `training/probe-artifacts`, current
hardware digest and real inventory guard. Its current hardware hash exactly matches
`training_guard.hardware_digest`: declared node name, actual `psutil.virtual_memory().total`,
actual GPU cores from `system_profiler`, and node agent version, canonicalized identically.
Unknown GPU inventory refuses preparation; no configured/fabricated GPU count substitutes.

The control app invokes `training_measurements.attach(app, supervisor)` and
`AccountedAdapterExtractor(...).attach(app)`. The latter subclasses the delivered
`AdapterExtractor` only to replace its independent quota calculation with the aggregate
ledger policy; its validation/copy/publication implementation is unchanged. Measurement
and extraction routes are absent from the data app. Every control route retains the node
bearer dependency. Feature-disabled mutation checks run AFTER authentication: unauthenticated
disabled requests are still 401. Disabled measurement prepare/inputs/start/lease/begin and
new extraction are 503; measurement stop/status and extraction history remain available.

Modern declared Studios instantiate artifacts, importer, analysis recovery, trainer,
measurement and extraction managers even with training disabled. The existing authenticated
artifact transfer/verification controls and peer-granted data endpoints remain available for
committed-state recovery. Both native watchdogs start before serving, independently of the
feature flag; failed observations are logged safely and retried without declaring vacancy.
Shutdown cancels/drains watchdogs and closes import preparation and both native journals.

### Measurement wire status and gateway dependency

The existing control routes mount at `/node/training/measurements/{attempt_id}`:
`prepare(TrainingMeasurementPrepare)`, `inputs(TrainingInputsRequest)`,
`start(TrainingStartRequest)`, `lease(TrainingLeaseRenewal)`, bodyless `begin`, `GET status`,
and `stop(TrainingStopRequest)`. Status/lease responses are `TrainingMeasurementNodeStatus`:
the owned `NodeTrainingStatus`, optional verified `TrainingMeasurementObservation`, stopped/
ready flags, actual training start timestamp and current swap/thermal/sample observations.
Extraction POST/GET mounts at `/node/training/adapters/extractions` and returns the existing
`TrainingAdapterExtractionStatus`; an extraction success is not registry publication or an
inference-ready proof.

`guard_measurement` inspects real engine ownership, discovers engine orphans, rejects image/
trainer/reservation occupancy and other measurement owners, and inspects native process argv.
Unowned MLX/VLM/image/trainer processes or denied process inspection refuse admission.
Memory experiments require an actually empty accelerator inventory. Coexistence checks the
exact ready instance/target set before consulting the optional lease hook.

**Remaining main integration hook:** `measurement_active_leases(instance_ids) -> int` must
read a fresh, authenticated current lease snapshot for the exact supplied local instance IDs.
It must return the actual active count, or raise `TrainingConflict` when unavailable/stale;
it must not perform a slow network round trip under the shared admission lock. A cached,
validated snapshot reader is appropriate. Missing callbacks, noninteger counts and nonzero
counts refuse coexistence preparation/start. Node proxy connection counters are deliberately
not used as substitutes: ordinary gateway streams use private engine ports and their lease
state is core-owned. Production defaults therefore still refuse coexistence until main supplies
this hook; memory mode has real local isolation checks and does not fabricate lease vacancy.

### Disjoint memory/disk/health accounting

All engine, image, conversion/reservation and ordinary training admission callbacks include
measurement `committed_bytes()`. Training/image mutual exclusion also covers measurements;
ordinary training cannot acquire a concurrent measurement slot. Availability callbacks exclude
only their OWN journal, allowing its durable admission to count itself exactly once.
Conversion disk admission includes trainer AND measurement filesystem holds.

`MetricsCollector.attach(..., measurements=...)` projects both native owners into the existing
bounded (max eight) training-status list, including their actual footprint observations.
Duplicate attempt ownership or an unbounded projection fails observation instead of quietly
deduplicating counted holds. Node committed memory adds engine/image/reservation/trainer/
measurement ownership once each; borrowed resident engines are not projected as trainers.
`swap_used_bytes` is `psutil.swap_memory().used`, with any observation failure or invalid
reading represented by `None`, never zero.

`TrainingDiskBudget` supplies a shared aggregate quota over private training state. For each
owned scope it counts the larger of its envelope and materialized bytes, while counting
unowned retained files separately. Trainer checkpoints/source directories, measurement
scratch artifacts, extraction staging/publication and importer staging/publication receive
disjoint projections. Quota roots are canonicalized; links, oversized/corrupt manifests or
uncertain filesystem observations refuse quota admission.

`AccountedArtifactImporter` uses the delivered transfer implementation with shared durable
metadata-memory and byte holds. Header/body preparation is reserved before transfer; each
retry receives FRESH scope IDs, rather than reusing a released idempotency key. Checkpoint
imports receive disk credit only from matching attempt/fence envelopes and their remaining
unmaterialized capacity. Existing retained bytes and extraction/import materialization are
not counted again on top of their owning envelopes. Cancel/shutdown shields an owned copy
through stream/filesystem completion before releasing its scopes; uncertain/abandoned work
keeps protected holds. Restart recovers only purpose-tagged import scopes with a valid durable
import intent, never arbitrary reservation IDs. Released scopes leave retained bytes counted.

The private reservation journal adds validated optional `owner_kind`, `disk_credit_bytes` and
`materialized_paths` metadata. Existing schema-v2 journals load with safe defaults. An older
agent that cannot read the added ownership metadata must fail closed for recovery rather than
advertise its holds as free; no shared wire/database model is changed here.

### Continuation verification

- Ruff check/format passed for the four owned source files and the two new test files.
- Strict mypy passed for `agent.py`, `metrics.py`, `reservations.py`, `routes/training.py`.
- Broad CPU node unit/contract gate: **643 passed, 1 existing platform-conditional skip**
  (`test_footprint.py`'s non-Darwin fallback on Darwin).
- New production-bootstrap contracts exercise actual control/data registration, bearer
  authentication with the feature disabled, authenticated pending measurement preparation,
  conversion/trainer refusal against its counted ceiling, health projection and stop proof,
  live disabled watchdog registration and cleanup. The test simulates Studio identity/hardware
  and empty inventory on CPU; it never supplies inputs, loads a model or starts Metal.
- Additional tests cover truthful/unknown swap, canonical hardware hashing, exact inventory/
  missing-active-lease refusal and callback scope, disjoint memory/footprint projection,
  aggregate quota without double-counting materialization, import retry scopes and cancellation
  retaining its hold until the owned copy completes.

No shared-model/database/worker edits, production edits, network changes, downloads or commits
were made in this continuation. Tests ran on the development/core host without model or Metal
work. Physical Studio measurement/coexistence and numerical acceptance are not claimed.

## Final native continuation: default lease cache and rank registration

This continuation supersedes the optional-only lease hook dependency above. Production
`serve()` now installs a real authenticated snapshot reader by default; the explicitly supplied
`measurement_active_leases` callback remains supported for controlled tests/integration.

### Lease producer/consumer interface

Main's core route is:

```text
GET /api/v1/internal/training/nodes/{node}/leases
Authorization: Bearer <existing node token>
X-Coire-Node: <declared Studio>
```

It must return the shared `NodeTrainingLeaseSnapshot(node, sampled_at, expires_at,
active_leases: dict[UUID, int])`. Include an explicit count, including zero, for EVERY instance
that may be addressed by the experiment. A missing key is not interpreted as zero.
Snapshots must have positive validity no longer than five seconds and nonfuture sample time.
The producer must authenticate/scope the node and compute actual current accelerator-request
leases; this node change does not implement or edit the core route.

`training/lease_snapshot.py` provides `TrainingLeaseSnapshotReader(settings, node=...)`,
`await start()`, a synchronous `reader(instance_ids) -> int`, and `await aclose()`.
The background client uses the declared `training_input_api_url` origin and
`core_control_host`, the existing node bearer, no redirects/environment proxies, identity
encoding, a two-second HTTP/operation timeout and a 64 KiB response bound. Wrong origins,
paths, response nodes, malformed/oversized responses or invalid counts cannot publish a cache.
No credentials or snapshots are persisted, and failure logs contain only node/error class.

Admission callbacks perform NO network I/O. Nonempty instance sets require every addressed
key and sum those exact counts; an empty set means an isolated memory experiment and sums
ALL leases in the node snapshot. Both isolated-memory and coexistence initial guards require
an actual fresh zero count in addition to their local ownership/inventory checks. The memory
guard no longer skips active-request admission merely because no model process is resident.

Cache authority expires using both the source timestamps and a monotonic deadline of at most
five seconds. Replaying an identical sample cannot extend authority; rewound observations or
rewrites of counts/expiry at the same sample time are rejected. Failed polls retain only the
last still-valid observation; expiry/missing observation refuses admission. Concurrent/repeated
start is serialized; stop cancels/drains polling, closes the client and invalidates the cache.
Production starts the reader outside the shared admission lock and closes it during shutdown.

### Distributed node constructors and mounts

Production now supplies:

```python
TrainingSupervisor(..., jaccl_hostfile=Path(settings.sharding_jaccl_hostfile))
RankImporter(
    training.components,
    Path(settings.node_state_dir) / "training" / "component-imports",
    port=settings.node_data_listen_port,
    max_bytes=settings.training_artifact_quota_bytes,
    disk_floor_bytes=settings.training_artifact_disk_floor_bytes,
    reservations=shared_reservations,
)
create_app(..., training_rank_importer=rank_importer)
```

The separate-app registration is equivalent to `TrainingComponents.attach(control, data,
importer=...)`: the control app has `state.training_components`, `state.training_rank_importer`
and the authenticated component control router; the data app has only the peer-granted
`/training-components/...` data router. Rank collection uses the control app's already attached
native supervisor. Component bytes do not acquire a control/fallback export path.
Rank imports use the real shared reservation ledger. Their client/transfers drain before
either native journal is closed, including failed-listener startup cleanup. Hidden component
directories and `component.json` ownership participate in aggregate retained-artifact quota
projection; materialized rank bytes are not added again to their owning trainer envelope.

The configured JACCL hostfile is only bound here. No hostfile generation, collective launch,
RDMA repair, address/firewall change or suspended fabric trial was performed.

### Final verification

- Ruff check/format and strict mypy passed for `agent.py`, `metrics.py`, `reservations.py`
  and new `training/lease_snapshot.py`; Ruff also passed for the changed/new tests.
- Focused native lease/registration/component/lifecycle/import/accounting gate:
  **103 passed, no skips**.
- Broader node unit/contract plus `tests/integration/test_training_distributed.py` CPU
  simulation gate: **684 passed, 1 existing platform-conditional footprint skip**.
- Lease tests exercise node-bearer headers, exact origin/path, wrong-node/path/auth/redirect/
  malformed responses, missing-instance refusal, all-node memory counts, timestamp/TTL limits,
  replay/rewind/clock rollback, concurrent startup, periodic polling and shutdown invalidation.
- Production-bootstrap contracts exercise both default authenticated reader and explicitly
  supplied reader, real rank-router registration/auth separation, configured JACCL binding,
  shared ledger identity, disabled-mode stop/history, and importer-before-journal shutdown.

Only owned node source, relevant tests and this evidence were changed. No shared contracts,
core route/database/workers, commits, production configuration or network permissions were
edited. All verification used CPU/simulated transports; no core Metal or real Studio workloads
were run. Hardware acceptance remains a separate feature gate.
