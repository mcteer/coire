# Reserved training measurement implementation and handoff

Date: 2026-10-05. Feature 016; constitution I, II, III, IV, V, VI and VII.

**Status: CPU control/contract and real PostgreSQL verification. No Studio resource,
MLX probe, or 15-minute hardware coexistence acceptance is claimed.** The production
mounts and workload callback below belong to the main owner. This contribution does
not enable the currently fail-closed admin measurement route.

## Implemented path

- `coire_api.training.measurements.submit_measurement()` audits and idempotently
  queues a typed recipe with immutable acquired base, split, analysis, tokenizer,
  template and runtime bindings. It applies configured recipe bounds. No caller
  supplies a pass flag or executable resource estimate.
- `coire_scheduler.training_measurements.admit_measurement()` takes the existing
  node admission locks. The initial memory experiment requires an isolated slot:
  no model/image/conversion/trainer hold or active accelerator request lease. It
  reserves the **entire remaining conservative slot**, including physical overage
  accounting, before node preparation. That ceiling is execution authority, not
  measured evidence. Disk holds include checkpoint/staging and private input bounds.
- The separate native `MeasurementSupervisor` uses the existing durable training
  journal, explicit argv, spawn nonce, PID/create-time, private input delivery,
  short execution lease and process-group death proof. A half-spawn is reconciled,
  never blindly replayed. Prepared baseline slots have a one-hour deadline; actual
  trainer processes retain the 30-second renewable execution lease.
- The fixed `coire_node.training.measurement` module refuses model work outside the
  two native Apple Silicon Studios. Core preparation/tokenization is refused.
  It calls the existing safe loader and unchanged `run_sft()`/bare mlx-lm trainer.
  Its watchdog samples kernel physical footprint, swap-used/swap-out counters,
  thermal pressure, lease and deadline before model work and throughout execution.
  Missing or unsafe observations fail closed. Sampling is nominally every 500 ms;
  the existing bounded thermal read may take up to its three-second timeout.
- Weight, adapter and evaluated optimizer tensor sizes, compiled input buffer size,
  MLX allocator peak, sampled physical footprint and complete serialized checkpoint
  bytes are observed. The existing checkpoint serializer is instrumented at entry,
  exit and by the concurrent sampler to record its own physical-footprint peak.
  Local measurement acknowledgements only let the experimental trainer continue;
  they are never submitted as durable checkpoints, adapters or recovery evidence.
  Serialized probe artifacts are scratch and are removed; input/scratch cleanup and
  disk release require proved process death.
- Coexistence requires current isolated memory evidence for the exact recipe,
  runtime and selected hardware first. The real gateway driver freezes prompt text,
  per-instance tokenizer-specific input lengths, arrival schedule, concurrency and
  output bounds. Every target distribution must reach 4,000 input tokens. Both
  phases use the same prompt set. Baseline and mixed phases each require 900 seconds
  and at least 100 completed streams **per exact instance**, with no failed requests
  or target substitution. The nearest-rank p95 matches `coire-ttft-v1`; a confirmed
  rolling latency breach stops the experiment. A failing baseline cannot start mixed
  training. The mixed phase must fall wholly within observed trainer execution.
  A recipe finishing early is inconclusive; the executor never invents updates or
  stretches a short run into passing evidence.
- All selected node observations are hashed into the immutable report. The measured
  conservative envelope adds an explicit safety margin and is hashed with hardware,
  runtime and configuration evidence using the existing resolver digest functions.
  Publication, profile creation and approval audit are one transaction. Unknown
  stops retain counted holds; interrupted phases are inconclusive and need a new
  administrator experiment. No manually asserted profile is created.

## Main-owner wiring signatures

```python
await submit_measurement(
    session, principal, body, idempotency_key,
    settings=settings, prompts=frozen_prompt_set_or_none,
) -> TrainingMeasurementReceipt

MeasurementNodeClient(settings, timeout=30.0)  # existing authenticated NodeClient
TrainingMeasurementExecutor(settings, transport, workload=None)
# start()/stop() are async; pass_once()/advance(UUID) are available for the worker.

GatewayWorkloadDriver(generate)
# generate is an authenticated gateway operation with this signature:
async def generate(
    principal: Principal,
    measurement_id: UUID,
    target: TrainingResidentTarget,
    prompt: TrainingMeasurementPrompt,
    max_output_tokens: int,
) -> TrainingMeasurementCompletion: ...

MeasurementSupervisor(
    journal,
    interpreter=pinned_interpreter,
    accelerator_guard=check_local_exact_inventory_and_active_leases,
    hardware_sha256=current_local_hardware_digest,
    store_root=registered_model_store,
    artifact_root=private_probe_artifact_root,
    memory_available=available_after_other_subsystems,
    disk_available=available_after_other_subsystem_disk_holds,
)
coire_node.routes.training_measurements.attach(control_app, supervisor)
```

The gateway operation must complete an actual authenticated, budgeted, leased stream,
measure the first real model-content token, discard output content, and return the
actual routed instance, exact target and observed token usage. It must authorize only
the current measurement's frozen resident set; ordinary unprofiled requests must not
bypass the training guard. A model-only HTTP success or inferred TTFT is insufficient.
The current public gateway response does not prove exact instance identity, so a
plain `/v1` HTTP request is deliberately not substituted for this callback.

The journal must share the node's admission lock. Include its `committed_bytes()`,
`held_disk_bytes()` and `statuses()` in every engine/image/conversion/training admission
and node health/footprint projection. Availability callbacks exclude this journal's
own holds, which its existing journal accounts itself. `accelerator_guard` must inspect
actual local ownership, including unknown processes, and reject active initial leases;
memory mode permits no other accelerator occupant. The hardware callback must derive
the same canonical digest as `training_guard.hardware_digest` from actual local node
name, physical memory, GPU cores and agent version. Start its `watchdog()` task and
close via `aclose()` after stopping the watchdog.

Typed node transport methods are `prepare(TrainingMeasurementPrepare)`,
`inputs(TrainingInputsRequest)`, `start(TrainingStartRequest)`,
`status(TrainingMeasurementPrepare)`, `renew(TrainingLeaseRenewal)`,
`begin(TrainingMeasurementPrepare)` and `stop(TrainingStopRequest)`.
They mount under `/node/training/measurements/{attempt_id}`. `attach()` installs the
existing node-token dependency on every route. Do not mount on a public or engine app.

The internal authenticated dataset content route must delegate measurement-grant
validation to:

```python
await authorized_measurement_source(session, dataset_id, node_name, secret)
```

`mint_measurement_inputs()` produces existing strict attempt-scoped input transports.
Only secret hashes enter `TrainingCommandRow`; the grant validator rechecks current
human/key authority, running measurement, addressed node, exact source, deadline and
frozen dispatch. This uses the existing dataset endpoint and no network change.

## Remaining prerequisites / integration boundaries

1. Wire the admin service, scheduler executor, authenticated node mount/watchdog and
   shared memory/disk/health accounting above. Delegate measurement input grants from
   the internal dataset route. Regenerate OpenAPI/TS after the main-owner route wiring.
2. Supply a production authenticated exact-instance gateway callback and frozen
   Studio-tokenized prompt metadata. Provide a recipe with enough genuine training
   work to cover the entire mixed phase, synchronized clocks, acquired/verified
   local assets and existing authenticated control paths. Do not infer these assets
   or token lengths on core.
3. Current native supervisor/trainer rejects coordinated two-rank checkpoints and
   mixtures. This contribution refuses those configurations at submission instead
   of claiming measured multi-rank support. Main-owner native rank/sampler work must
   land before extending that gate and testing all physical ranks.
4. Existing worker input validation currently hashes splits with
   `sha256(split.model_dump_json())`, while API resolution and this service use
   canonical `payload_digest(split)`. Reconcile that pre-existing shared boundary in
   the main-owned worker/resolver before native delivery; do not translate an
   immutable split identity in the measurement executor to hide the mismatch.
5. Regular training admission still needs the main-owner exact coexistence-profile
   consumer and scoped measurement-workload lease path. Connect measurement outcome
   metrics to the feature's dashboard/alerts. Outcome labels include `unknown`,
   `inconclusive`, `succeeded` and node `observed`; correlation IDs stay in logs/spans.
6. Run actual Studio memory and coexistence matrix, cancellation, core-loss/lease,
   restart/uncertain-spawn and cleanup acceptance using the tested build. No builds,
   deployments, network changes, commits or Studio workloads were performed here.

## Verification

CPU unit/contract and shared-contract selection: **70 passed** (including exact-stream
identity/counting, per-target duration/sample refusal, resource substitution/swap/
thermal refusal, full-slot persistence, immutable node config and authentication).

Disposable **PostgreSQL 17** integration: **1 passed**. Exercises audited idempotency,
changed-intent rejection, competing admissions with one atomic winner, full conservative
hold size, node-bound secret-hashed grants and expiry, unknown-stop hold retention,
and transactional profile publication after a synthetic stop proof. Synthetic resource
observations in this test are explicitly CPU fixtures, not hardware evidence.

Strict mypy: **passed, 11 files**. Ruff lint and format freshness: **passed** on the
owned source, measurement-only shared models and tests.
Tests emit the existing `/run/secrets`-absent warning in the isolated development
environment; no secrets or production configuration are read or created.

## Follow-up: mixture and two-rank measurement matrix

The main owner has wired the admin service, production gateway callback, node
measurement accounting/snapshots and worker registration. The native worker now
uses `split_digest()` consistently and supplies the shared mixture compiler and
full per-rank state serializer. Items 1, 2 and the split-hash item 4 above describe
the original handoff, not current missing work.

### Implemented extension

- Measurement resolution now freezes **all** training and independently selected
  held-out sources, invokes the existing model-free mixture compiler, and verifies
  sample pools, replacement quotas and cross-source split leakage. No stale
  single-source/epoch-size/replacement restriction remains in this service.
- Native probes use `load_all_frozen_inputs()` through `probe_inputs()` and pass the
  complete source set to the real `compile_samples()` implementation. Cleanup
  includes each generated source subdirectory. CPU input allocation accounting
  traverses sampler indices, source dataclasses and actual cached objects.
- Two-rank admission uses the existing measured JACCL link eligibility and validates
  the deployment-mounted generated hostfile. Both participants share one job,
  attempt, fence, runtime, collective and **full** resource envelope. The smaller
  safe node slot is reserved identically on both nodes; weights and optimizer state
  are never divided by two. Source/staging disk holds account for every source.
  Same-node resident target checks use the appropriate local subset on each rank.
- Both starts are dispatched concurrently after both preparations/input deliveries.
  Memory two-rank probes, as well as coexistence probes, wait for both loaded workers
  before the core sends the start-boundary signal. The inherited native supervisor
  supplies the pinned JACCL launch environment; the measurement constructor accepts
  optional `jaccl_hostfile` and `jaccl_coordinator_port` and otherwise uses the
  existing configured hostfile/default port.
- `RankMeasurementCheckpoint.checkpoint(state, adapter, optimizer_state) -> None`
  calls the actual `CheckpointStore.save_rank()` serializer, verifies the resulting
  files/header/state with `verify_directory()`, writes the real component descriptor,
  and measures their actual byte sizes. The two workers exchange only strict inert
  summaries with `BareMeasurementRankExchange`: one fixed 4-KiB frame per rank over
  the already initialized bare CPU all-gather. There are no invented file entries,
  full-bundle manifests, peer tensor transfers or durability acknowledgements.
- Both summaries must share the same evaluated update and immutable execution
  identity, retain the actual local component hash and agree on a common pair hash.
  The largest **common sum of both measured rank states**, rather than the larger
  local component alone, determines `checkpoint_bytes`. An independent 60-second
  `DeadlineGuardian`, watched with lease/footprint/swap/thermal/deadline guards,
  bounds blocked collectives and checkpoint boundaries. Completed scratch is removed.
- Observations include rank, world size and the actual common sizing summaries;
  report construction requires both physical nodes and identical common sizing
  evidence. `TrainingMeasurementDispatch` rejects separate attempts, differing
  resolved envelopes and missing/duplicate rank identities.
- Embedded `TrainingMeasurementRequest.prompts` is the canonical source when present;
  a conflicting prompt transport is rejected. Tests explicitly retain the current
  `prompts: null` wire field and verify that adding prompt metadata changes the
  request/report hashes while leaving the same resource observations unchanged.

### Concrete remaining interface prerequisite

**The main-owned `run_sft()` still lacks the non-durable measurement hook at the
time of this verification.** Its existing checkpoint callback requires a complete
common manifest and durable acknowledgement, which cannot honestly represent
independently measured local rank scratch. No forbidden worker/checkpoint/supervisor
or agent file was edited to bypass that requirement.

Requested main-owner extension:

```python
measurement_checkpoint: Callable[[CheckpointWorkerState, dict[str, Any], Any], None] | None = None
```

At the existing evaluated checkpoint boundary, this callback must be mutually
exclusive with the durable `checkpoint` callback. It must retain the existing
state capture, bare collective/guardian checks and coordinated pause/stop behavior,
but must not manufacture a manifest, emit a full-checkpoint staging event or require
a durability acknowledgement. Keep the unchanged upstream `train()` loop.

The measurement runtime detects that exact hook and passes
`RankMeasurementCheckpoint.checkpoint`, the existing `BareCollective` and its
independently watched guardian. Until it exists, native prepare refuses before any
hold or model load. New authenticated `GET /node/training/measurements/capabilities`
reports code/inventory capability only; it advertises world size two only with that
hook and valid local generated JACCL inventory. API submission checks both nodes'
current capability/hardware identity and refuses unavailable two-rank experiments
**before queueing**. Disabled training advertises no execution sizes. Capability is
not numerical or RDMA acceptance; actual measured link eligibility is still required
at admission, and actual native collectives are still required during execution.

### Follow-up verification

- **74 CPU/unit/contract tests passed**, including shared contracts. New matrix tests
  stage three real private source files (two train, one separate held-out), run the
  actual mixture compiler/samplers on both logical ranks, restore deterministic
  cursors, serialize and verify full synthetic CPU safetensors for each rank, and
  prove that both actual serialized sizes feed the common resource envelope.
- **4 disposable PostgreSQL 17 tests passed**: prior idempotency/grant/death/publication
  tests, full mixture/replacement/held-out resolution and grants, capability refusal
  before queueing, and atomic two-rank admission with one shared full envelope on
  unequal safe node budgets. The JACCL link in the admission control-flow test is
  explicitly a fixture; it is not a hardware or numerical success claim.
- Strict mypy passed for the 11 selected owned/shared-measurement files. Ruff lint
  and formatting passed on the owned source/tests. Existing shared worker/sampler/
  checkpoint definitions are outside this contribution's edit ownership.

No real Studio training, native two-rank collective, numerical comparison or
15-minute coexistence acceptance was run. The suspended fabric prerequisite in
`distributed-evidence.md` remains operationally binding; no address, route,
firewall, deployment or network setup was changed, and no commit was made.
