# Feature 016 — scheduler runtime integration evidence

Date: 2026-10-05. Branch: `feat/016-sft-training-jobs`. Uncommitted work preserved.

## Scope and governing evidence

Read AGENTS.md, constitution 2.0.0, architecture/roadmap, feature spec/plan/tasks,
execution record, and controller/native/extraction/distributed/measurement/operations
evidence. This contribution edits only the assigned API executor, NEW
`training/runtime.py`, `training/input_grants.py`, `routes/internal_training.py`,
`coire_scheduler/workers.py`, NEW runtime tests and this record. The pre-existing
DatasetAnalysisExecutor registration in workers is preserved.

No shared contracts, controller.py/training_controller.py, agent.py, node modules,
production/network configuration, models or core engine workloads were changed.
No commits, deployments or Studio workloads were performed. Verification databases
are disposable PostgreSQL 17 containers bound to loopback and removed with volumes.

Constitution I/II: authenticated Studio commands only; core holds metadata, no tensor
bytes or engine imports. III: existing strict input/extraction/artifact/status models.
IV: current authority, participant/fence checks and hash-only source grants. V: frozen
acquired base and independently verified adapter copies, initially private/unverified.
VI: content-free runtime spans and existing baseline metrics poller lifecycle.
VII: actual Postgres transactions plus typed simulated-node boundaries; simulation
does not establish native inference, numerical, physical-copy or cluster acceptance.

## Implemented

- `mint_attempt_inputs(session, prepare, settings)` returns existing typed
  TrainingInputsRequest for every frozen source. Grant secrets are memory-only;
  TrainingDatasetGrantRow stores SHA-256, node/attempt/source identity, size and expiry.
  There is no secret-bearing input request in the command journal.
- Source authorization rereads live user/key authority, job/attempt state, fence,
  execution/lease deadline, persisted immutable prepare, exact participant/rank/
  reservation/request identities and successful source/split/analysis bindings.
  Both mint and download require current immutable ready inputs.
- Internal source routing selects the grant category before authorization. A failed
  analysis/training authorization cannot fall back to measurement permissions;
  measurement-only hashes delegate to authorized_measurement_source.
- Prepare dispatch sends the immutable prepare, commits hash-only grants, delivers
  inputs with POST 202, and polls the same prepare for readiness. Pending receipts
  remain dispatching without immutable receipt reduction. Subsequent controller
  ticks refresh grants; cancellation/revocation fences later minting and reduction.
  No transaction holds database locks across node network I/O.
- Checkpoint-commit consumes shared NodeTrainingStatus from POST 200 and checks
  node/job/attempt/fence; its journal stores the actual typed status rather than a
  manufactured 204 acknowledgement. Extraction POST/GET are concretely typed.
- NEW TrainingRuntime supplies the controller's four real callbacks. Final extraction
  persists a deterministic native command and counted disk hold before I/O, validates
  committed full-checkpoint/base/runtime lineage, polls the real extraction route and
  returns only a node-produced manifest. Replay preserves command/artifact identity.
- Adapter mirroring uses authenticated grants/import/status/refresh/independent verify
  calls. Each complete-copy proof is checked by record_verified_copy. Grants travel
  only in memory; import journals hold strict secret-free intents and actual receipts.
  Promotion extraction source is taken from its succeeded command, rather than assuming
  it is always the trainer's rank-zero node.
- Smoke preparation records deterministic instance UUID
  `uuid5(adapter_id, "validation-smoke")`, with adapter metadata
  `smoke_instance_id`. Existing placement owns admission/holds/engine commands.
  Runtime polls NodeClient.get_engine; only actual bare-engine READY status reaches
  finalize_serving_adapter, whose exact target/digests/PID/create-time/freshness/held
  reservation checks remain authoritative. It never sets adapter ready itself.
- A separate bounded promotion poller progresses explicit node.adapter.extract
  commands and private extracted candidates even when their source job is terminal.
- TrainingRuntimeWorker registers in SchedulerWorkers regardless of the enable flag.
  The controller, promotion poller and poll_training_metrics are owned and stopped
  before client close. Registration uses local controller imports, avoiding the
  training_executor/training_controller cycle. Disabled execution refuses prepare/
  start/lease dispatch, leaves queued work undispatched, and sends existing owned work
  through durable cancellation/reconciliation rather than renewing it.
- Guard reads actual persisted TrainingMetricSample, participant footprint and fresh
  ledger memory/thermal/health observations. Missing/stale measurements return
  insufficient_samples. Confirmed memory/thermal breaches return their real reason.
  Resume-profile lookup validates persisted successful report hashes, envelope,
  base/runtime identity, expiry and current hardware, never an asserted pass callback.

## Verification

Focused/broadened runtime gate:

```text
COIRE_INTEGRATION=1 uv run pytest -q \
  apps/coire-api/tests/integration/test_training_runtime.py \
  apps/coire-api/tests/unit/test_training_runtime_transport.py \
  apps/coire-api/tests/integration/test_training_controller.py \
  apps/coire-api/tests/integration/test_training_measurement_transactions.py \
  apps/coire-api/tests/integration/test_training_baseline_metrics.py \
  apps/coire-api/tests/unit/test_training_guard.py \
  apps/coire-api/tests/unit/test_training_metrics.py --tb=short
48 passed, no skips, 27.00 s
```

New runtime cases cover pending/refresh/ready transitions, secret-free journaling,
current authorization and ready-input loss, wrong-node/category refusals, cancellation
during network preparation, real extraction intent/disk journaling and polling,
independent adapter verification receipts, reserved exact-target reducer finalization,
cancel-before-finalization refusal and persisted metric/thermal/missing-data guards.
Transport tests cover typed POST 202 inputs, POST/GET 200 extraction, typed POST 200
checkpoint acknowledgements and disabled baseline/client shutdown ordering.
Synthetic node receipts are explicitly test fixtures, not hardware measurements.

Focused Ruff check/format and strict mypy pass on the five owned source modules and
two NEW tests (7 files). Isolated Settings instances emit the existing absent
`/run/secrets` warning; no production credentials are used.
After the final immutable extraction/import and profile-evidence checks, reran the
two NEW runtime files: **20 passed, no skips, 14.10 s**. Focused Ruff/mypy and
`git diff --check` passed again.

## Concrete integration blockers — completion is not claimed

1. **Private validation placement resolver:** current central resolve_target requires
   adapter.state == ready; `_candidate_nodes` and load dispatch call it. Therefore
   the real smoke instance currently cannot launch while validating/replicating.
   The target/placement owner must add a dedicated admin-only validation resolver
   scoped to `adapter.metadata_record["smoke_instance_id"] == str(instance.id)`,
   current adapter authority, both verified exact manifests and the finalizing job
   (or explicit promotion authority). Public resolve_target must still require ready.
   Runtime does not temporarily mark an adapter ready or widen public resolution.
   Positive finalization tests simulate placement's held member and actual typed
   READY receipt; they do not prove this blocked launch path.
2. **Protected live guard data:** there is no existing per-exact-instance live TTFT
   sample reader or persisted swap delta in the inspected gateway/ledger boundary.
   Historical measurement samples are insufficient. Runtime returns
   insufficient_samples for resident chat, so it cannot approve mixed auto-resume;
   latency-breach monitoring and full protected resume are not complete.
   Required owner interface is an authenticated/internal reader returning each
   protected instance's timestamped TTFT seconds and observation time (coire-ttft-v1,
   trailing 5 minutes, >=30 samples, <=60 s freshness), plus current node swap-used/
   swap-out baseline/delta and fresh footprint/thermal identity. Bind it to the
   current exact resident multiset and attempt/profile, with no aggregate/base-only
   fallback. Shared fields/contracts belong to the main owner.
3. **Controller follow-through:** the existing controller must invalidate breached
   profile evidence and consume the protected live guard reader for confirmed latency
   pauses/new-profile resume. That module is owned by the admission/controller agent.
4. Native/tiny-model/physical-copy/two-rank/coexistence gates and final integrated
   image/schema/export checks remain feature-level acceptance, not this CPU gate.
   The distributed evidence's missing component/collective contracts are unchanged.

No tasks were checked complete and no full-feature acceptance is inferred from this record.

## Continuation — rank collection and resolved owner interfaces (2026-10-05)

The earlier private-smoke and live-guard blockers above are **resolved by the owner
implementations** in `smoke-evidence.md` and `coire_scheduler/training_guard.py`.
Read the updated distributed evidence and native component/store/router implementations
before this continuation. Shared models/migration, node modules and controller modules
remain owner-managed. The only `training.py` change is the permitted
`ingest_training_event` rank-event branch, delegating to the NEW coordinator module.

### Implemented collection path

- NEW `coire_scheduler/training_components.py` persists each authenticated rank
  descriptor in a deterministic per-artifact/per-rank TrainingCommandRow. It checks
  current authority, live job/attempt/fence/lease, participant node/rank, immutable
  resolved/runtime identity, configured update bound and monotonic rank updates.
  Replays must match the exact immutable component. No new table or wire type.
- Existing native mailbox ingestion remains contiguous and idempotent. Rank events
  cannot impersonate full checkpoints or advance the committed recovery pointer.
- Concrete TrainingNodeClient methods implement authenticated control GET metadata,
  POST verify/grant, POST 202 import/refresh, GET status, POST cancel and POST 200
  collection with the existing shared contracts. Node/import/command/component
  identities and full verification receipts are checked; protocols use their actual
  HTTP statuses. Core never downloads component/tensor bytes.
- The coordinator verifies each source's local metadata and full component receipt,
  issues a bounded grant, asks the opposite node to import, polls actual status,
  refreshes failed partial transfers within the original deadline, and independently
  verifies the destination. Both A-to-B and B-to-A orientations must finish before
  either collection command is sent. Native RankImporter performs the actual bytes
  transfer over declared data fabric, without core/control fallback.
- Hashes, metadata, deterministic import/verification/collection intents and actual
  receipts are journaled before/after I/O; grant secrets remain transport-memory-only.
  Every privileged dispatch rechecks current authority/fence. Lost acknowledgements
  and scheduler restart reuse exact intent IDs; pending transfers never imply success.
- Both nodes receive the same two immutable descriptors only after both local
  components have independently verified receipts. Native workers then assemble and
  emit the complete bundle before waiting for core acknowledgement. This preserves
  the native ordering and avoids a mirror-before-event deadlock.
- `mirror_checkpoint` now requires both full-bundle mailbox events for world size
  two, equal canonical full-manifest hashes, and exact equality to all accepted rank
  files/states/runtime/resolved identities. Only afterward can existing complete-copy
  verification reducers commit the new recovery point and enqueue both-rank acks.
  A one-sided bundle or substituted file leaves the prior checkpoint intact.
- Rank loss, revocation, identity corruption or the fixed 60-second collection
  deadline queues stop for both ranks and cancels owned peer imports. Stop intent,
  transfer status and job labels do not release trainer holds; only existing actual
  process proofs do. Repeated failures do not reset the recovery timestamp/event.
  Committed historical component rows are excluded from the bounded scan so they
  cannot starve a later checkpoint. Old-fenced/stopped import work is also drained.
- `bind_collective_prepares` freezes the generated JACCL digest, resolved runtime and
  native declared coordinator port 32323 for both pending prepare intents under one
  job lock before either network dispatch. It uses the scheduler's configured local
  deployment-mounted `settings.sharding_jaccl_hostfile`, bounded to 1 MiB, validates
  the two declared `.fabric` hosts/native topology with existing sharding helpers,
  and requires current measured JACCL eligibility. It does not read a remote node
  file, generate a hostfile, guess devices or reconfigure the fabric. Frozen bindings
  are replayed; already-dispatched prepares cannot change. Prepare-only `collective`
  is explicitly excluded from TrainingInputsRequest payloads.

### Worker/callback integration

- TrainingRuntimeWorker injects `training_guard.guard_reason` and
  `training_guard.resume_profile` directly. The incomplete local implementations were
  removed; compatibility methods delegate to those owner implementations. They use
  actual exact-instance DB TTFT/swap/profile evidence and invalidate breached profiles.
- The component coordinator shares the owned training client and is started/stopped
  with the controller even when training is disabled.
- The worker owns a separate MeasurementNodeClient and registers the real
  `TrainingMeasurementExecutor(settings, client,
  GatewayWorkloadDriver(gateway_measurement_generate(settings)))`. While disabled it
  scans only already-running, persisted dispatches and routes them through the existing
  executor's interrupted-measurement stop/reconciliation path. It never admits queued
  experiments. Components/controller/measurement tasks stop before their clients close;
  the existing baseline metrics poller remains enabled independently of the feature flag.
- New component/recovery spans, bounded operation metrics and content-free correlation
  logs use existing telemetry helpers. Feature baseline alerts/dashboard remain those
  recorded by the operations owner; no new production configuration was edited.

### Meaningful verification

NEW test file is uniquely named
`apps/coire-api/tests/integration/test_training_component_coordinator.py`, avoiding the
native component contract test's module name. The initial combined collection exposed
that filename collision; it was corrected, not bypassed by changing pytest checks.
Earlier typed transport fixtures were corrected to exclude prepare-only `collective`.
The private smoke owner's trainer-stop/hold-release corrections were preserved.

The new disposable-Postgres cases exercise two authenticated typed control endpoints
with explicit test-only bearer tokens and scoped transfer secrets. Both orientations,
local/destination verification, collection barriers, pending-transfer restart, lost
collection acknowledgement replay, exact scopes and monotonic identity refusal,
cancellation/revocation/missing-rank stop barriers, prior-checkpoint preservation,
actual complete-bundle reducer commitment/both acknowledgements, missing-link refusal
and frozen generated-file binding are checked. The generated hostfile and successful
link projection in the positive binding case are **synthetic fixtures**, not RDMA
hardware evidence. Disabled measurement-recovery SQL selects only running dispatches;
unit cases verify real guard callback identity and both client/worker enable states.

Final broadened command:

```text
COIRE_INTEGRATION=1 uv run pytest -q \
  apps/coire-api/tests/integration/test_training_component_coordinator.py \
  apps/coire-api/tests/integration/test_training_runtime.py \
  apps/coire-api/tests/unit/test_training_runtime_transport.py \
  apps/coire-api/tests/integration/test_training_controller.py \
  apps/coire-api/tests/integration/test_training_admission_controller.py \
  apps/coire-api/tests/integration/test_training_transactions.py \
  apps/coire-api/tests/integration/test_training_measurement_transactions.py \
  apps/coire-api/tests/integration/test_private_adapter_smoke.py \
  apps/coire-api/tests/unit/test_training_authorization.py \
  apps/coire-api/tests/unit/test_training_guard.py \
  apps/coire-api/tests/unit/test_training_measurements.py \
  tests/integration/test_training_targets.py \
  tests/integration/test_exact_adapter_resolution.py \
  apps/coire-node/tests/contract/test_training_components.py \
  tests/integration/test_training_distributed.py --tb=short
157 passed, zero failed/skipped, 146.09 s
```

This includes actual native ASGI synthetic-byte component transfer contracts, exact
adapter/private smoke and run-token regressions, owner admission/guard/controller
fixtures and measurement transactions. Development Settings emit the existing
absent `/run/secrets` warning. No production credentials, model or Metal work.
Focused Ruff check/format and strict mypy passed on the six source modules and three
owned tests (9 files). No shared contract/migration, production/network/model edits,
commits, task-checkbox changes or real-cluster acceptance claims.
After adding the explicit duplicate/monotonic-rewind/immutable-descriptor case and
missing-generated-file assertion, the final three owned test files were rerun:
**37 passed, zero failed/skipped, 25.81 s**. Focused Ruff/mypy and `git diff --check`
passed again. The broader 157-case result above precedes that additional test case;
no later source behavior change invalidated that regression run.

### Remaining physical gate

Real two-rank training remains gated on a deployment-managed native-generated JACCL
hostfile with matching digest on both Studios and the scheduler's declared copy,
current measured JACCL eligibility and working native collective/coordinator evidence.
Those prerequisites were not provisioned or measured in this contribution. The
suspended fabric helper was not run. Synthetic hostfile/metadata/receipts cannot
enable or prove physical RDMA, numerical resume, physical full-copy durability or
the real training/coexistence acceptance matrix.
