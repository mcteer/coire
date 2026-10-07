# Controller execution evidence — feature 016

## Current continuation — admission, guards and reverse ownership

The original record below is historical. The earlier missing memory-evidence
contract and competing-admission fixture failure are now resolved. This
continuation implements the expanded user-assigned workstream; it does not claim
real Studio/numerical/coexistence acceptance.

### Implemented and verified

- The competing-admission fixture now calls
  `tests/training_measurement_fixtures.py::persist_measured_profile`. It stores
  strict measurement, memory evidence and profile rows with matching config,
  hardware, report and envelope digests. Values are explicitly **synthetic CPU
  protocol fixtures**, not measurements of a real model or Studio. Admission is
  not mocked and its evidence gate is not loosened.
- Admission validates persisted typed reports, source/runtime/config identity,
  current hardware, expiry, safety and per-rank conservative bounds. Compatible
  newly measured evidence may approve the unchanged frozen upper bound; the job
  resolution is not replaced with new defaults or a new estimate.
- Actual counted model holds are projected into exact **instance + variant +
  adapter** resident identities. Unknown, legacy and non-ready occupancy cannot
  masquerade as a measured match. Duplicate instances remain distinct.
- Measured coexistence requires the exact current resident multiset, both
  15-minute workload-bound phases, >=100 samples per instance per phase, matching
  per-instance count/p95 summaries and immutable report identity. New mixed
  admission additionally requires the live sample floor/freshness checks.
- Eligible idle single-node victims must have a ready owned engine, one member,
  no pin, no in-flight request/active lease and a finite idle policy. Sharded,
  unknown and protected occupancy is not independently evicted.
- Ordered node locks cover both nodes before inserting any participant hold.
  All victim draining transitions, restoration intents, unload commands,
  full per-node pending holds and the fenced attempt commit together. Neither
  prepare nor start is enqueued before every victim's authoritative unload,
  stopped instance and released hold are confirmed. Current physical occupancy
  remains counted throughout the drain.
- After all-rank process proof, eligible victims receive deterministic exact-target
  replacement instance offers through ordinary placement. Original instances are
  not resurrected. Captured policy/target/pin plus audited mutation identity
  prevents newer pin cycles, placement changes and retirement from being undone.
  Capacity-blocked offers remain pending and are retried by the controller.
- `training_guard.guard_reason` and `training_guard.resume_profile` are public
  async runtime callbacks. Protected TTFT uses PostgreSQL `percentile_disc(0.95)`
  over the trailing five minutes, per exact instance/target, with >=30 completed
  samples and newest sample <=60 seconds old. It excludes unknown token counts,
  >4000-token prompts, nonfinite/negative duration, future timestamps and failed
  requests. Request duration is never substituted for first-token duration.
- Confirmed latency breaches invalidate the admitted profile. Missing/stale
  observations remain `insufficient_samples`. Memory and thermal breach detection
  remains independent of latency sampling.
- Reverse image/conversion/sharded admission checks training ownership under the
  same node locks. A last-mile SQLAlchemy `before_flush` fence in placement
  service covers every ORM new/reactivated/increased MODEL/IMAGE/CONVERSION hold,
  including legacy/direct single-node writers. It uses the adapted transaction's
  PostgreSQL advisory locks; release, reconciliation and pin observations remain
  usable. No engine side effect occurs before this commit gate.
- Gateway leases reject draining victims and unmeasured new resident identity.
  Existing exact already-admitted serving instances retain chat priority during
  protective teardown and under-sampled traffic. This allows genuine traffic to
  replenish the window; it does not assert healthy latency or authorize new mixed
  training/resume. Fresh sampled eligibility remains mandatory for those admissions.
- Prober residency includes actual native training footprint exactly once, marks
  missing footprint unknown, updates only owned current-fence participant
  footprint observations, and preserves the authenticated `sampled_at` timestamp.
  Stale/future/naive samples cannot become fresh merely because core polled them.
  Training joins physical overage and ledger drift accounting. Conversion and
  sharded capacity calculations now include physical overage.

### Runtime wiring and precise shared prerequisites

Main registration/extraction/private smoke integration remains main-owned. Wire:

```python
from coire_scheduler.training_guard import guard_reason, resume_profile

TrainingExecutorTransport(
    client,
    extract_final_adapter=runtime.extract_final_adapter,
    prepare_adapter=runtime.prepare_adapter,
    guard_reason=guard_reason,
    resume_profile=resume_profile,
)
```

Both callbacks accept their existing positional ID and an optional keyword-only
`session: AsyncSession | None = None` for domain transaction/testing use. The
default owns a committing `session_scope()`.

At final inspection, `UsageRecordRow` still has no `instance_id`,
`first_token_at` or `first_token_duration_ms`; it has only whole-request duration.
Main must add those nullable fields (plus `(instance_id, first_token_at)` index)
in the existing **0031** migration/shared usage contract and persist actual
first-token observations from gateway execution. Legacy nulls must stay null,
not be backfilled from request duration. Positive PostgreSQL TTFT-query and
healthy measured mixed/resume acceptance are still blocked by this concrete
dependency; the reader currently refuses missing timing observations.

Main must also add nullable `swap_used_bytes >= 0` to authenticated
`NodeStatus`/`NodeStatusV2` and `NodeMemoryLedgerRow`, in the same migration, and
populate native swap observations. Prober and guard consumers are implemented;
admission records per-node baseline swap in `training.admission.evidence` metadata.
Missing swap remains insufficient evidence, not zero or healthy. These shared
files and `runtime.py` were not edited in this workstream.

### Actual final checks

Targeted Ruff check/format: **passed; 14 files already formatted**.
Targeted mypy: **Success: no issues found in 14 source files**.

The final test run used `COIRE_INTEGRATION=1`, the repository's disposable
PostgreSQL 17 helper, and a second disposable PostgreSQL DSN for the existing
independent-connection image lock test (plain `postgresql://` format). Both
containers were scoped to loopback and disposed by the helper. No production
database or Studio was targeted.

```text
pytest -q -ra
  apps/coire-api/tests/integration/test_training_admission_controller.py
  apps/coire-api/tests/integration/test_training_transactions.py
  apps/coire-api/tests/integration/test_training_controller.py
  apps/coire-api/tests/integration/test_training_measurement_transactions.py
  apps/coire-api/tests/unit/test_training_guard.py
  apps/coire-api/tests/unit/test_nodes_prober_image_memory.py
  apps/coire-api/tests/unit/test_nodes_prober_version.py
  apps/coire-api/tests/unit/test_placement_policy.py
  apps/coire-api/tests/unit/test_image_admission.py
  apps/coire-api/tests/unit/test_acquisition_workflow.py

85 passed, 10 warnings in 43.14s
```

No skips or failures in the final run. Warnings were existing missing development
secrets-directory warnings. New real-Postgres admission tests cover drain/prepare
barriers, lease and pin races, reverse-allocation races, pin-cycle/new-policy-aware
restoration, exact-instance multiplicity, all-or-none two-node full-envelope
admission, stale/fresh physical observations and protected chat priority.

The first broader regression run found a DSN-format prerequisite error in an
existing image lock test; converting the disposable asyncpg URL to that test's
required plain PostgreSQL DSN resolved it without changing the test. Earlier
implementation failures were fixed before the final green run.

Additional authorized files changed in this continuation:
`placement/service.py`, `gateway/proxy.py`, `nodes_prober.py`, scheduler
`image_dispatch.py`, `sharded_instances.py`, `acquisition.py`, the shared test
measurement fixture helper, the competing admission test fixture, and NEW
`test_training_admission_controller.py`. `training_recovery.py` now implements
restoration; the controller polls pending offers. No shared schema/migration,
training API service, executor, workers or main edits, network configuration,
model weights/Metal work, Studio tests or commits were performed.

## Initial workstream record (historical)

### Scope and status

This record covers the scheduler controller workstream only. Implementation is
**not full feature acceptance**. In particular, successful extraction/mirroring/
reserved adapter inference, mixed admission, eviction/restoration and positive
automatic-resume acceptance remain unverified or unfinished as listed below.

Read `AGENTS.md`, constitution 2.0.0, architecture/roadmap and feature spec, plan,
tasks, data model, research, quickstart and node protocol. Requirements checklist:
16 checked, zero unchecked. No extension hooks file exists. Feature branch was
already selected by the prerequisite script. No commits or Studio operations.

Files edited in this workstream:

- `apps/coire-api/src/coire_scheduler/training.py`
- `apps/coire-api/src/coire_scheduler/training_admission.py`
- `apps/coire-api/src/coire_scheduler/training_guard.py`
- NEW `apps/coire-api/src/coire_scheduler/training_controller.py`
- NEW `apps/coire-api/tests/integration/test_training_controller.py`
- This NEW evidence record.

`training_recovery.py` was read and verified with the focused checks but did not
need edits. Shared contracts, API training services, executor, workers and main
were not edited by this workstream. Existing concurrent work was preserved.

## Implemented controller behavior

- Worker-compatible `start()`/`stop()`, `run_once()` and explicit `tick(job_id)`.
  PostgreSQL rows/command UUIDs are the execution journal; there is no in-memory
  source of execution authority. Default scan cadence is one second; configured
  polling and control I/O must each be positive and at most five seconds.
- Independently progress up to eight durable nonterminal jobs, prioritize
  cancelling/pausing jobs, and send stop/pause commands before observation work.
  Per-job/per-rank I/O failures cannot prevent other ranks' stop attempts.
- Commit a visible preflight transition, then perform measured-evidence admission,
  full per-rank holds, prepare/start barriers and immutable command replay.
  Two-rank admission uses the existing measured `link_projection().tp_eligible`
  gate rather than an unconditional two-rank refusal.
- Recheck the frozen resource envelope through the trusted resolver and require
  currently valid persisted evidence covering the actual chosen Studios. A
  stored resolution alone does not authorize unmeasured resource admission.
- Drain typed native mailbox pages using persisted contiguous cursors; attempt,
  node, event sequence and authenticated status identities are checked. Mirror
  staged bundles before reducing final worker stop status.
- Use existing fenced checkpoint/receipt reducers and all-rank process proofs.
  A partition or an expired core lease is never interpreted as death. Unknown
  status enters coordinated recovery with holds still counted. Expired leases
  request teardown rather than resurrecting execution authority.
- Renew leases through the existing command lane. Preserve explicit recovery
  checkpoint selection, metric rollback and step-zero/recovery-limit behavior
  implemented by the existing domain functions.
- Guard-confirmed memory/thermal/latency breaches persist protective pause and
  request checkpoint pause. The existing 60-second deadline escalates to stop;
  process proof precedes reservation release. Admin pauses remain manual.
- Protective resume has a 60-second cooldown, requires injected fresh measured
  profile/guard reads, validates persisted profile validity, current authority,
  frozen inputs/resource evidence, no active old attempt and remaining cumulative
  execution time. Missing/ineligible evidence leaves the job paused.
- Complete final checkpoints with proven stopped trainers enter finalization.
  Automatic extraction has deterministic checkpoint-derived adapter/command IDs
  and a persisted extraction intent. Pending extraction is bounded to 60 seconds.
  Node-produced shared manifests go through `stage_serving_adapter(automatic=True)`;
  actual reserved exact-target smoke receipts go through `finalize_serving_adapter`.
  The existing job lock/authority checks arbitrate cancel versus final output.
- Controller operations emit existing content-free OTel spans/metrics and bounded
  structured diagnostic codes. No tensors, engine imports, credentials or raw
  node exception text are introduced on core.

## Exact registration and interface handoff

The main agent owns registration in `workers.py`/`main.py`, and node-client
lifecycle. Under the existing default-off training gate, append a
`TrainingController` to the scheduler workers. Stop the controller before closing
its node client.

```python
transport = TrainingExecutorTransport(
    client,
    extract_final_adapter=extract_final_adapter,
    prepare_adapter=prepare_adapter,
    guard_reason=guard_reason,
    resume_profile=resume_profile,
)
worker = TrainingController(transport)
```

Both classes are exported from `coire_scheduler.training_controller`.
`TrainingController` accepts keyword-only `sessions` (a callable returning an
async context manager for a committing `AsyncSession`), `poll_interval_s=1.0`
and `io_timeout_s=5.0`. The default session factory is `session_scope`.

`TrainingExecutorTransport` implements these existing adapters:

| Method | Existing implementation |
| --- | --- |
| `dispatch_command(command_id: UUID) -> None` | `dispatch_training_command(command_id, client)` |
| `training_events(node: str, attempt_id: str, after: int) -> NodeTrainingEventPage` | `TrainingNodeClient.training_events` |
| `training_status(node: str, attempt_id: str) -> NodeTrainingStatus` | `TrainingNodeClient.training_status` |
| `mirror_checkpoint(checkpoint_id: UUID) -> bool` | `mirror_checkpoint(checkpoint_id, client)` |

Four injected async methods still require real integration:

1. `extract_final_adapter(checkpoint_id: UUID, adapter_id: UUID,
   command_id: UUID) -> TrainingArtifactManifest | None`.
   Use the exact supplied command and adapter identities, persist native extraction
   intent before I/O, authorize the checkpoint/base, account disk, poll idempotently,
   and return only an authenticated node-produced serving manifest. `None` means
   durably pending; it is not a successful artifact or a reason to manufacture one.
   Controller extraction intent operation is `training.final.extract`; it is not
   a node wire payload and must not be dispatched by a generic node command scanner.
2. `prepare_adapter(adapter_id: UUID) -> tuple[UUID, EngineStatus] | None`.
   Mirror/independently verify both immutable adapter copies, reserve and execute
   a real exact-target Studio inference smoke. Return `(instance_id, smoke_status)`;
   the domain finalizer independently checks actual instance/member/held reservation,
   process identity, target digests and freshness. `None` means durably pending.
3. `guard_reason(attempt_id: str) -> TrainingReason | None`.
   Read real memory/swap/thermal/progress and per-target latency telemetry. Confirmed
   breaches return their typed reason. For resume eligibility, insufficient/stale
   samples must return `insufficient_samples`, not healthy `None`. The latency
   helper uses nearest-rank trailing-five-minute p95, 30 samples, 60-second freshness
   and the `coire-ttft-v1` definition. Insufficient samples alone do not pause a
   currently running attempt as a fabricated latency breach.
4. `resume_profile(job_id: str) -> TrainingProfile | None`.
   Return a persisted current profile matching the exact candidate combination
   with fresh healthy live evidence. A latency-invalidated combination requires
   a new measured profile. No manually asserted eligibility or estimated evidence.

Alternatively inject an object implementing the complete
`TrainingControllerTransport` protocol directly (as the database tests do).

## Actual verification

The integration fixture uses the repository's disposable **PostgreSQL 17 Docker
container**, bound only to loopback with a generated password and removed with
its volumes on completion. It does not target production PostgreSQL or Studios.
Controller fixtures and transports are explicitly synthetic metadata and process
receipts, **not measured model memory, Metal, network or performance evidence**.

Final focused command:

```text
COIRE_INTEGRATION=1 uv run pytest -q \
  apps/coire-api/tests/integration/test_training_controller.py \
  apps/coire-api/tests/unit/test_training_guard.py
17 passed in 13.06s
```

This is 15 real-Postgres controller cases and two existing guard cases, with no
skips. Coverage includes restart replay, live re-adoption without another start,
concurrent cancel reducers, timeout uncertainty, protective pause/forced stop,
terminal exclusion, unknown liveness, expired lease refusal, queue timeout,
deterministic pending final extraction, cancellation blocking extraction,
extraction deadline, both-rank stop barriers and manual/protective pause gates.

Targeted `uv run mypy` over the four assigned existing scheduler modules, new
controller and new test file: **Success: no issues found in 6 source files**.
Targeted Ruff check: **All checks passed**; Ruff formatter applied to those files.

The initial broadened run additionally included
`apps/coire-api/tests/integration/test_training_transactions.py`:
**31 passed, 1 failed in 24.28s**, with no skipped tests. The failing
test was `test_competing_admissions_count_the_full_envelope_once`. Its fixture
passes a synthetic resolved envelope directly into submission but stores no
successful measured profile. With fail-closed admission, both competing admissions
return `None`; the old assertion expects one admitted job. The missing evidence
fixture was not weakened or presented as measured. This failure was unresolved
at the initial handoff and is now resolved by the continuation above.

## Concrete unfinished work and limits of this evidence

- Registration and the four real transport integrations above belong to the main
  agent. They must not be replaced with success stubs. The controller's extraction
  and smoke pipeline is implemented but successful finalization is not proven by
  this workstream's tests; its tests prove pending/failure/cancel journal behavior.
- At inspection/verification time, `TrainingMeasurementResult` has **no
  `memory_evidence` field**, while `resolve_submission` requires it. Thus no
  persisted report can currently satisfy the strict measured-resource resolver.
  Complete the shared measurement contract and real reserved measurement/ingestion
  path; update the admission-race fixture with valid synthetic protocol evidence
  clearly labeled as a simulation. Actual model capacity still needs actual
  measured acceptance, not that fixture.
- `admit_training` still conservatively queues resident accelerator model holds.
  Full measured mixed-workload admission, atomic eligible-victim draining and
  version-aware eviction restoration are unfinished. There is no claim that
  existing pinned chat coexistence or eviction/reload capability is shipped.
- Positive measured protective auto-resume, all-rank prepare/start, checkpoint
  corruption/fallback and successful final smoke through the complete controller
  need end-to-end integration evidence once their dependencies are connected.
  Existing domain transaction tests cover several underlying primitives; this
  does not substitute for complete controller acceptance.
- Node-local watchdog/control timings, real engine numerical recovery,
  all-rank execution, physical mirrored copies and mixed-chat measurements were
  not run here, per this workstream's explicit prohibition on real Studio tests.
- No task markers were checked or feature-completion claim made from partial
  evidence. Constitution I/II/III/IV/V/VI/VII remain the governing requirements.
