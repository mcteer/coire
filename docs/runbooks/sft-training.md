# SFT training operations

Feature 018 v3 DPO/ORPO procedures and current qualification scope are in
[Preference training](preference-training.md). Existing v1/v2 SFT recipes retain
their wire shapes and hashes. Feedback publication/withdrawal procedures are in
[Feedback operations](feedback.md).

Feature 017 declared evaluation procedures are in
[Durable evaluations](evaluations.md). Existing v1 recipes retain their held-out
loss behavior. Evaluation-owned checkpoint pauses require complete stop/cleanup
proof before resume and fresh training admission. Use “Keep paused after
evaluation” for an explicit administrator override; do not manually release
uncertain reservations or checkpoint pins. Evaluation scores do not rewrite
training success or grant user WRITE verification.

Feature: [016 specification](../../specs/016-sft-training-jobs/spec.md),
[plan](../../specs/016-sft-training-jobs/plan.md),
[execution evidence](../../specs/016-sft-training-jobs/execution-record.md),
[operations evidence](../../specs/016-sft-training-jobs/operations-evidence.md),
[ADR 0012](../adr/0012-sft-training-boundaries.md).

**Status: runtime acceptance passed for the supported matrix below.** Exact results and
final build gates are recorded in the execution evidence. Training defaults off and
requires authenticated administration and a current exact measured profile.
Use the generated OpenAPI for the deployed version before invoking a route. Route implementations
in the working tree do not establish deployed scheduler/node compatibility or successful execution.

Disabling `COIRE_TRAINING_ENABLED` blocks new admission and resume. Authenticated history,
events, metrics, checkpoints and measured-profile reads remain available, as do pause/cancel and
terminal deletion requests. Disabling admission alone is not proof that a trainer stopped: preserve
the reconciliation/stop lanes and counted holds until owned process-group death is confirmed.

## Supported scope and pending matrix

| Configuration | Intended capability | Acceptance status |
| --- | --- | --- |
| One Studio, dense unquantized base, explicit linear LoRA targets | SFT, Adam/AdamW, held-out loss | Live dense LoRA train/pause/resume/mirror/gateway inference passed; restart, full-state recovery, cancellation and verification gates passed |
| One Studio, acquired affine 4-bit/group-64 base, QLoRA | Same, frozen quantized base | Live recipe -> three physical mirrored checkpoints -> adapter gateway inference passed; rank/link/transfer and verification gates passed |
| One Studio, dense unquantized base, explicit linear DoRA targets | Includes magnitude tensors | Live dense DoRA train/pause/resume/mirror/gateway inference passed; restart, full-state recovery, cancellation and verification gates passed |
| Two Studios, affine 4-bit/group-64 QLoRA | Data parallel; full weights/optimizer envelope on **each** rank | Live completion, rank-0/rank-1 loss and bounded data-link recovery passed; expiry/refresh, revoked mid-transfer grant and corrupt-byte imports passed; partial copies stayed private |
| Two Studios, dense LoRA or DoRA | Requires its own exact measured profile and native matrix | Unmeasured; no supported configuration claimed or approved for admission |
| Resident chat + training, including pinned ops model and exact adapter targets | Only exact current measured profiles; chat first | Both 15-minute phases passed: 180 completions/target/phase, zero failures/swap; baseline p95 0.533/0.538 s, mixed 0.473/0.487 s; injected protective guards passed |
| Images + training on one Studio | Mutually exclusive | Both live admission directions passed; newer-pin-aware eviction restoration passed |
| Visual SFT, preference objectives, full fine-tuning, quantized DoRA/unsupported MoE targets | Refused | Outside 016 |
| Adapter failover, sharded adapter serving, remote dataset loaders, Hub dataset imports | Refused | Outside 016 |

Do not enable a combination based on a unit test or an estimated weight size. Require a current
measured full-envelope profile for exact hardware/runtime/base/parameterization/world-size/batch/
sequence/rank settings. Mixed profiles also fix the multiset of resident variant+adapter instances,
workload and p95 query identity. Identity changes, a guard breach, or seven-day expiry invalidate
evidence. Missing/stale/under-sampled evidence blocks admission; it is not a healthy measurement.

An isolated memory measurement can remain queued when the node's thermal sample is `unknown`,
even with ample memory and no model holds. Inspect the node/ledger thermal field before changing
capacity settings. On macOS builds without IOPMrootDomain's `ThermalPressureLevel`, compatible
node builds read Apple's public `NSProcessInfo.thermalState` through the native Objective-C ABI.
Its nominal/fair/serious/critical values map to the existing thermal contract; unavailable or
unrecognized readings remain `unknown` and block admission. Update the frozen node environment
through staged verification and controlled activation, then wait for a fresh health sample.
Do not edit ledger thermal values or approve a profile manually to clear the queue.

For a hardware-fingerprint refusal, compare the authenticated measurement capability digest with
the digest of the current registered node inventory. The node package version advertised by health
and registration is authoritative; the shared SERVICE_VERSION default is not the node version.
Use a compatible frozen build rather than editing evidence or database hardware fields to match.

For accelerator-inventory refusals, check the authenticated admin engine inventory for orphaned
test workloads before unloading an identified leftover through the audited engine route. On macOS,
some OS daemons protect argv from the node service account. Compatible builds classify these only
with kernel-verified valid platform code, readable noninterpreter executable metadata and stable
process birth identity. Unknown/interpreter/unsigned processes remain protected occupancy. Do not
ignore AccessDenied globally or change the node service user to make a measurement pass.

A preparation refusal before local reservation can leave a core dispatch awaiting stop proof.
Compatible nodes persist a fenced no-start rejection in a pristine attempt namespace and expose
the existing stopped/status receipt, permanently preventing that attempt from later starting.
The scheduler's bounded stop lane can rebind the original prepare and request its exact stop proof.
If local evidence is missing, corrupted or conflicts, keep the counted reservation and inspect
reconciliation; never release it directly in Postgres because an HTTP command failed.

### Shared request stalls and diagnostics capacity

If all resident targets slow at the same time, use gateway and native proxy spans to distinguish
ownership lookup, upstream headers and streaming. Authenticated node requests propagate the
standard trace context; gateway failures retain their original request and closed failure reason.
A failed or incomplete workload never approves a profile. The native watchdog and reconciliation
inventory run off the request loop; inventory sampling does not hold the ownership lock.

On macOS, native metrics probes use `posix_spawn`, close inherited descriptors by default and pass
only a bounded PATH/LANG environment. Production sampling identified gRPC fork handlers holding
the Python GIL during the older periodic ioreg probes. This was corrected without disabling
telemetry, lowering measurement requirements or changing numerical training code. Roll native
builds through the frozen environment workflow after every owned trainer has stopped.

Tempo has a fixed 1 GiB container ceiling and `GOMEMLIMIT=768MiB`; inspect its health, OOMKilled
flag and restart count when traces become unavailable. Preserve the trace volume during recreation.
Its recovery does not establish a training or latency pass; use the independently retained workload
report and durable job/audit history.

Admission uses the shared ledger and atomic ordered node locks. Pins, active requests, sharded
groups, sandbox/failover slices and image work remain protected. Only eligible idle single-node
instances can be evicted; reload must respect newer pin/retire/placement decisions. Impossible fit
is refused with per-node shortfall; temporary contention queues with a reason and finite deadline.
Data parallelism cannot fit a base too large for one Studio. One trainer slot per node; planned
queue bounds are eight jobs globally, four/admin, 24-hour queue and 72-hour active execution limits.
Actual limits come from [typed settings documentation](../../deploy/compose/README.md#sft-training-settings-feature-016-implementation).

**Network incident:** both original Thunderbolt bridges, addresses, membership and peer routes
recovered on 2026-10-04 as recorded in the execution record. Live direct-NIC trials remain suspended.
Do not run the retired/suspended interface-move helper or repeat raw bridge-member changes.
Recovered bridges are not evidence of JACCL training success or persistent direct-NIC configuration.
Real two-rank QLoRA and the operator-armed bounded link interruption passed on the restored fabric;
transfer expiry, interruption and corruption gates passed using isolated native copies. Use authenticated
`GET /api/v1/admin/network/links/studios` to observe declared link state. No control-fabric artifact
fallback, firewall widening or live network trial is authorized by this runbook.

### Installed interpreter identity and peer data access

The frozen node environment must resolve to the existing dedicated `coire-node-python` executable,
not uv's shared `python3.13`. Check without loading a model:

```bash
/opt/coire/envs/current/bin/python3 -c 'import sys; from pathlib import Path; print(Path(sys.executable).resolve())'
```

On the measured installation the expected target is
`/opt/coire/python/cpython-3.13.15-macos-aarch64-none/bin/coire-node-python`.
An enabled Local Network entry alone does not prove the service uses that executable. On Studio B,
the old shared-runtime binding returned BSD errno 65 while the existing dedicated executable and
native clients reached the exact peer data listener. Root success likewise does not establish
eligibility for the unprivileged node service.

Installer environment identity `coire-node-runtime-v2` includes executable bytes as well as frozen
wheels/requirements. The installer verifies the requested executable binding before native smoke
or publication, including reuse of an existing candidate. After provisioning Python under the
declared prefix, it omits `UV_PYTHON_INSTALL_DIR` and `UV_PYTHON_BIN_DIR` only during the explicit
`uv venv --python` call: uv 0.12.7's managed-install shortcut otherwise substituted `python3.13`
even though its creation message named the dedicated executable.

Rebuild/stage through the corrected installer rather than retargeting an active environment's
symlinks. The service-preserving activation helper rejects candidates bound to a legacy/shared
runtime. Verify candidate ordinary BSD and `DataFabricClient` connectivity as the installed service
user, then activate with operator-root and recheck authenticated node data-link health. A 404 from
an intentionally nonexistent peer artifact proves HTTP reachability only, not a scoped successful
artifact copy. Actual two-copy checkpoint commitment remains a separate acceptance gate.

## Datasets, loss and provenance

Admin-uploaded, uncompressed UTF-8 JSONL supports `text`, `prompt_completion` and `conversation`.
Text supervises all non-padding targets. Prompt/completion and conversations default to the final
assistant target; earlier turns/tool results are context. Structured tools must have valid call/
response relationships; tool-only assistant turns are supported by the canonical schema. Images,
malformed tools, incompatible templates, overlength and zero-target examples are refused before
accelerator admission; there is no silent truncation or dropped invalid row. Metadata is provenance,
not supervision. Consult shared dataset contracts for the exact wire fields; no executable loader,
archive, caller filesystem path or automatic download is accepted.

`POST /api/v1/admin/datasets` registers an immutable revision. `GET` list/detail and
`POST /api/v1/admin/datasets/{dataset_id}/analyze` expose asynchronous tokenizer/template-specific
analysis. Readiness requires full-row validation and matching successful Studio analysis. Rerunning
analysis creates a distinct identity; it never rewrites source bytes. Inspect bounded row/field/code
diagnostics without echoing private rows into tickets or telemetry. Preserve source digest, declared
provenance/permission, split seed, tokenizer/template/base-manifest and analysis identities.

Splits group canonical content hashes, excluding IDs/metadata, so exact duplicates cannot cross
train/validation. Default split is 95/5 with both pools nonempty. Mixtures use immutable revisions,
sample counts, positive normalized proportions, strategy and seeds; largest-remainder quotas plus
deterministic ordering/RNG define draws without a merged corpus. `replacement=false` must fit each
source pool. Conflicting duplicate memberships across mixture sources fail preflight. Resume restores
sampler cursor/permutation/epoch and RNG, not just the original seed.

Planned upload bounds: 256 MiB/file, 1,000,000 rows, 1 MiB/row, 100 displayed diagnostics; 20 GiB
core dataset quota and 2 GiB free floor. Studio CPU analysis reserves 1 GiB and has a 30-minute
deadline. Verify enforcement and deployed settings before enabling uploads. Referenced queued,
running, recovering and paused inputs remain pinned. `DELETE /api/v1/admin/datasets/{dataset_id}`
must refuse live references; terminal provenance remains readable after purge with an explicit
non-reproducible status. Failed staging is subject to the 24-hour orphan sweep, not blind directory
deletion. Inspect unresolved cleanup receipts rather than declaring bytes reclaimed at request time.

## Observe

Use current admin authority, the documented authenticated gateway, Keychain-backed credentials,
and the approved browser Origin for browser mutations. Audit failure refuses privileged mutations.
Never put credentials, rows or raw engine output in commands recorded as evidence.

Durable routes (independent of optional Tempo/Loki/Grafana history):

| Read | Purpose |
| --- | --- |
| `GET /api/v1/admin/training/jobs` | Paginated states, queue/recovery reasons and versions |
| `GET /api/v1/admin/training/jobs/{job_id}` | Current intent, attempt and immutable settings |
| `GET /api/v1/admin/training/jobs/{job_id}/metrics` | Persisted losses/progress by attempt/update; follow cursor, max 2,000/page |
| `GET /api/v1/admin/training/jobs/{job_id}/events` | Replay; SSE with `Accept: text/event-stream` and `Last-Event-ID`; accept reset snapshots |
| `GET /api/v1/admin/training/jobs/{job_id}/checkpoints` | Retained state, copy verification and lineage |
| `GET /api/v1/admin/ledger`, `/api/v1/admin/nodes`, `/api/v1/admin/engines` | Holds and process/node observations |
| `GET /api/v1/admin/audit` | Security/mutation history, regardless of diagnostics mode |

### Native lease snapshot lane

The node's production reader calls
`GET /api/v1/internal/training/nodes/{node}/leases` with its Keychain-sourced node bearer and
`X-Coire-Node: {node}`. The header, credential and path must identify the same declared Studio;
human admin/session credentials cannot read this internal lane. It remains available when new
training admission is disabled. The response is `Cache-Control: no-store` and expires five seconds
after its database observation, taken after acquiring the node admission lock.

`active_leases` explicitly includes zeroes for current instances, including engine-only and sharded
membership and retained protected holds. Only unreleased, unexpired request leases contribute actual
counts. Legacy/unowned model occupancy contributes a conservative nonzero UUID marker; unbound active
leases also remain visible. Isolated memory probes sum the entire mapping, so such ownership cannot
be mistaken for a vacant accelerator. Ambiguous ownership or more than 256 identities refuses the
snapshot rather than truncating it. A missing/stale/refused observation blocks native admission.

Inspect the existing admin engine/instance/ledger views to resolve unknown ownership and look for
`coire.api.training.lease_snapshot` spans and structured `training node lease snapshot` logs. Restore
API/node version compatibility or the declared node secret through the normal deployment workflow;
do not fabricate a zero inventory to clear a refusal. During rollback, disable new training admission
and retain reconciliation/cancel lanes; an unavailable snapshot remains fail-closed.

Compose maps `COIRE_TRAINING_ENABLED` to `TRAINING_ENABLED` for API and scheduler.
The implemented route gate preserves authorized history/checkpoint/profile reads,
pause/cancel and terminal deletion when disabled. New submission/validation,
resume and measurements remain gated. Keep reconciliation, baseline observation
and stop lanes running while disabling admission; a flag is never process-death proof.
Verify deployed-version behavior before draining and explicitly unpublish adapters
through audited controls; admission disable is not adapter retirement.

Stall alert: over 300 seconds without a completed update on a running attempt, sustained one
minute. This is an operator diagnostic threshold, not proof of process death; long legitimate updates
may need inspection. A different job's progress cannot clear it. Check execution phase, heartbeat,
latest update/checkpoint, transfer state and holds before cancelling. Finite rising loss is not
infrastructure failure; nonfinite loss must fail safely while retaining prior durable history.
Attempt replay/discarded segments remain labeled; never flatten resumed loss into fictitious continuity.

## Kill and protect chat

Use `POST /api/v1/admin/training/jobs/{job_id}/cancel` with
`Idempotency-Key: <unique-operation-key>` and JSON `{"expected_version": <current-version>}`.
Pause and resume use the corresponding `/pause` and `/resume` suffix with the same typed body.
Retry the same key/body only for the same action; on version conflict reread state and decide a
new action. A 202 command receipt is not stop proof. Poll detail/events and ledger until all owned
process groups are confirmed stopped and holds released exactly once. Cancellation is terminal;
browser disconnect is not cancellation. Do not use the generic acquisition-job delete endpoint as
a substitute for training cancel, and do not release a reservation manually to make capacity appear.

Healthy cancellation budget is five seconds. Pause should commit a complete two-copy checkpoint
within sixty seconds or report forced stop and the last saved recovery point. These are required
acceptance targets, not measured results here. Node-local watchdog checks at most one second and
the renewable 30-second lease must stop unauthorized orphan work on core loss; runtime verification
remains a gate. Keep the kill lane and node watchdog available during draining.

Memory/swap growth, footprint above the reserved envelope, serious/critical thermal state or a
confirmed per-target chat first-token p95 breach require protection. Chat latency uses nearest-rank
`coire-ttft-v1`, five-minute per-target window, >=30 samples and <=60-second freshness; >1.5 seconds
is a breach. Missing samples block new mixed admission, not fabricate a latency breach in an active
attempt. Guard evaluation <=5 seconds and deadline enforcement must not depend on Alertmanager.
`CoireTrainingProtectionOverdue` means stop/pause proof is missing after the deadline, not merely a
successfully handled guard trip. Inspect reason and owned process state; keep uncertain holds counted.
Ledger drift >10% while training is active merits inspection; this does not establish that training
caused drift. Training footprint inclusion in the existing ledger metric still needs verification.

## Recover and fence

On restart first reconcile the owned command/attempt/fence, PID and creation time. Re-adopt a verified
live trainer; never respawn because a core request timed out. Unknown liveness remains recovering
with counted `HELD`/`RELEASING` memory. Check node journal and authenticated scheduler/node observation.
For two ranks, establish termination/fencing for **both** before another attempt starts. Do not
claim a network partition is death proof, clear a hold manually, or kill an unrelated reused PID.
There is no verified public force-fence route in the reviewed training API: if automated fencing
cannot provide proof, leave the job recovering and escalate through the node lifecycle operator
workflow; do not invent a bypass command. Release needs a tested reachable-node fence/stop path and
documented unreachable-node isolation proof before admitting replacement work.

Select the newest fully verified common checkpoint. Corrupt newest state falls back only to a
valid retained bundle; absent compatible state produces a refusal. Require unchanged world size,
runtime, immutable datasets/template/base/settings and current admin authority. Restore optimizer,
schedule, RNG and sampler state together. Before the first checkpoint, report explicit step-zero
restart, not resume. Never feed weights-only upstream scratch saves into recovery. Admin pauses
require explicit resume; protective pauses may resume only after cooldown and fresh admission
evidence, with new measurement after latency invalidation. Failed/cancelled jobs require a new run.

## Checkpoints and retention

The existing admin Jobs page now includes training runs with current state/reason, completed
updates, persisted latest non-rolled-back losses and counted reserved memory. Its confirmed Stop
uses the normal audited/versioned training cancel lane. Recovering/uncertain runs continue showing
their held/releasing memory; only confirmed release removes it from the summary. A stale version
reports a conflict without silently replaying a changed command. History and Stop remain readable/
available with new training disabled. `/api/v1/admin/console/training-activity` is the bounded,
content-free projection; it excludes original recipes, data and paths and checks current admin
authority. The run link opens full persisted history, including attempt boundaries.

Durable checkpoints require full rank manifests, every file independently verified on **both**
Studios, and a fenced core commit. An unavailable peer blocks advancement after bounded waiting;
preserve the last durable point. Checkpoint replication alert fires once pending commit age exceeds
sixty seconds. Inspect peer availability, scoped grant expiry, copy receipts, storage free space and
corruption through authenticated paths; do not copy tensors through core/control-fabric fallbacks.

Planned bounds: every 100 completed updates and pause/final boundaries; retain latest three with
at least one valid recovery point, <=20 GiB/job and <=200 GiB training/artifact store per Studio,
20 GiB free floor. Preflight must fit two complete checkpoint envelopes including staging/mirror
space. Retention cannot delete live references, the latest complete recovery point or promoted
serving artifacts. Pause/fail visibly on exhaustion rather than delete protected state.

The scheduler's `training-retention` lane remains running with training disabled. It journals
exact per-copy delete commands, removes retiring copies from readiness, and retries uncertain
node acknowledgements. A checkpoint becomes `purged` only after both copy receipts prove erasure;
its metadata and promoted-adapter lineage remain. Current local cleanup conservatively waits for
all native trainer ownership to be released. Inspect `node.artifact.delete` command state and
`coire_training_artifact_cleanup_total{outcome="unresolved"}` for deferred erasure. Active serving
adapters, transfer grants, extraction/import work, linked files and missing reference observations
refuse local cleanup. Do not manually erase directories or release disk holds after a timeout.
Interrupted purge remains in a private `.deleting-<artifact-id>` directory with an immutable
deletion journal. Retry the original command; changing its identity does not authorize a new purge.
Attempt scratch/input/component cleanup and resulting aggregate envelope release still require
their own receipts; an artifact-copy receipt alone does not free the entire attempt disk hold.

`DELETE /api/v1/admin/training/jobs/{job_id}` uses an idempotency key and expected version and is
for terminal retirement/cleanup, not stopping a live trainer. Follow cleanup receipts to confirmed
purge; retained adapter lineage must survive. Checkpoint promotion is a distinct audited action,
not automatic publication after cancellation.
`POST /api/v1/admin/training/checkpoints/{checkpoint_id}/promote` is available. Its typed 202 receipt
is followed through adapter detail to ready after native extraction and verification.
Adapters default admin-only/unverified. `PATCH /api/v1/admin/adapters/{adapter_id}` changes visibility
with optimistic version; `DELETE` retires. Exact-pair harness evaluation is required for write use;
base verification never transfers. Unpublish/retire prevents new selection without substituting a base.

## Preserve and back up

Before cleanup/rollback, export authorized job details, recipes, resolved/input/runtime identities,
attempt/update loss history, checkpoints/copy receipts, adapters and audit to private external
artifacts; paginate and retain checksums. No tensor files or private datasets belong in Git.
Capture a consistent Postgres backup and the private core dataset volume with the existing backup
workflow. `pg_dump` contains metadata, **not Studio checkpoint/adapter tensors**.

Drain/checkpoint and confirm stop/fence before snapshotting each Studio's node-owned immutable
artifact store. Back up complete manifests and tensors plus compatible versioned runtime identity,
preserving file digests and copy receipts. Validate restore in an isolated environment against
manifests and metadata references; verify optimizer/RNG/sampler recovery and exact adapter serving.
Two replicas are availability, not independent backups. Simultaneous Studio disk loss is outside
the resume guarantee without an independently tested private artifact backup. No automated backup
route is provided. A private metadata dump, dataset archive and selected Studio tensor archive
were restored into isolated locations and verified in feature-016 acceptance; see the execution
record for exact checksums and scope. An independent off-host artifact backup remains an
operator requirement for simultaneous Studio disk loss.

## Disable, drain and roll back

1. Freeze new training submissions, measurement/admission and automatic recovery/resume dispatch;
   also stop new adapter publication/selection through audited visibility changes. Keep reads,
    pause/cancel, observation and watchdog control live. Use `COIRE_TRAINING_ENABLED=false`
    with the compatible API/scheduler. Its controller cancels active/finalizing work through
    owned stop commands while retaining observation and reads; paused jobs without an active
    trainer remain preserved. Stopping the scheduler before reconciliation can strand uncertain holds.
2. Pause to a durable checkpoint or cancel each job using versioned authenticated controls. Wait
   for every rank's stop/fence proof; preserve counted uncertain reservations. Verify restore intents
   against current pins and placement before reload; do not undo newer administrator policy.
3. Preserve/export/back up metadata, uploaded data, complete Studio artifact stores, manifests and
   audit. Record immutable image/node/runtime digests and compatibility versions. Confirm backup
   restore in isolation; never delete production lineage to make a downgrade pass.
4. Only after drain/preservation disable the current feature gate. Roll service/node versions back
   through their deployment-managed pinned image/versioned-environment workflow. Old nodes must
   refuse unknown target/checkpoint versions; adapter instances must not become base-only instances.
   Keep the additive schema if the compatible application rollback supports it.
5. **Safest migration outcome is refusal.** Migration `0031_sft_training` refuses downgrade with
   any nonterminal training job; any retained adapter/checkpoint/artifact-copy/dataset revision;
   any non-null exact-target UUID field (including usage `variant_id`); or any non-null chat/MCP
   target JSON field. Even terminal training and ordinary populated target projections may block
   rollback. Do not weaken guards, drop tables, null fields or purge lineage to force a downgrade.
   Prefer compatible application rollback with schema retained; otherwise restore the validated
   pre-upgrade backup in isolation and plan an explicit reviewed data migration. A clean disposable
   downgrade/upgrade trial does not authorize destructive production downgrade.
6. Verify base chat/image/run/registry/failover regression behavior and authenticated diagnostics;
   only re-enable after the full capability and compatibility gates pass. Record actual timings,
   output identities and failures in the feature execution record. The 2026-10-07 acceptance
   rolled API `473c28bb` back to compatible `05223944` and Studio A `0.2.0-d6a27507f84e`
   back to `0.2.0-511c4a911f79`, then restored both. Authenticated history, retained
   checkpoint verification, base chat and exact-adapter chat passed in both phases.
   This is an additive-schema application rollback; production migration downgrade was not run.

## Baseline wiring

The existing helpers emit `coire_training_events_total{kind}`,
`coire_training_metric_samples_total{kind}`, `coire_training_observations_refused_total{reason}`
and `coire_training_operations_total{operation,outcome}`; node workers also define completed-update
and stop counters. Event/sample traffic cannot show per-job stall age, queue depth or durability.
`TrainingBaselineMetrics` now emits the gauges below from the persisted-state
reducer in `coire_scheduler/training_metrics.py`. An isolated actual OTLP-to-collector
test verifies their Prometheus names and 21 bounded state/reason/scalar series.
**Scheduler lifespan integration remains required**; file existence is not deployed emission.

| Required gauge | Semantics and bounded labels |
| --- | --- |
| `coire_training_snapshot_timestamp_seconds` | Epoch time of last successful durable-state/guard refresh; no labels; emit even when disabled |
| `coire_training_jobs{state}` | Durable nondeleted job counts, closed state enum, explicit zeroes |
| `coire_training_progress_oldest_seconds` | Maximum age since last completed update (or running start before first update) across running attempts; no labels; zero when none |
| `coire_training_recovery_oldest_seconds` | Maximum continuous unresolved recovery/unknown ownership age; no labels; zero only after resolution |
| `coire_training_checkpoint_pending_oldest_seconds` | Maximum age of outstanding checkpoint two-copy commit, including overdue blocked replication; no labels; zero when none |
| `coire_training_guard_overdue{reason}` | Count of unresolved pause/stop-proof deadline violations; closed memory/latency/thermal/lease/cancel reason enum; zero after proof |

Integration interface (scheduler owner; never create this task in API/MCP):

```python
from coire_scheduler.training_metrics import poll_training_metrics

# After configure_telemetry() and init_engine(), alongside existing lifespan tasks:
training_metrics_task = asyncio.create_task(poll_training_metrics(stop))
# On shutdown use the existing stop event, then await before dispose_engine():
stop.set()
await training_metrics_task
```

`poll_training_metrics(stop, *, interval_s=5.0, emitter=None)` runs independently
of the feature enable flag and DBOS/worker registration; interval must be >0 and
<=15 seconds. `load_training_metrics(session)` is available for one refresh and
requires a **fresh session** because its first statement sets REPEATABLE READ,
READ ONLY. All series publish atomically only after the successful read/transaction.
Before first success, series are absent; failures preserve snapshot age and log
no exception/SQL/source text. Stop/cancel receipts alone do not clear violations:
every rank needs stored `stopped_at` plus immutable `stop_proof`.

Progress uses only positive-update, non-rolled-back training samples from the
attempt (never validation or another attempt). Recovery/state ages use the
earliest retained event in the current continuous state segment, with persisted
`updated_at` as the fallback. Unknown ownership lacks a dedicated transition
timestamp in the current schema: its age conservatively starts at attempt creation.
The scheduler owner must persist state-transition events consistently and avoid
updating `updated_at` for repeated unresolved observations without an event; this
is necessary for exact continuous recovery/pause timing. No new contract/migration
was introduced by this operations slice. Expired leases and old fenced attempts
remain visible until all-rank proof; job terminal labels alone do not clear them.

Refresh from persisted state and node proof, at least every 15 seconds; timestamp advances only on
successful observation, not poll start. Multiple jobs must not mask the oldest unresolved condition.
Update all zero series, preserve ongoing ages across restarts, and reconcile stale/dead attempts.
No job/attempt/model/adapter/dataset/user IDs, selectors, digests, prompt/row content or arbitrary
error strings in labels. Correlation IDs belong in authorized history or content-free spans/logs.
The existing placement gauge `coire_placement_ledger_drift_ratio` is reused; verify trainer physical
footprint accounting before claiming the training memory signal is complete.

`CoireTrainingBaselineUnavailable` warns on missing required gauges or snapshot age >60 seconds
for one minute. Missing data must never become `or vector(0)` health. Baseline alerts aggregate away
resource labels and link back here; their timings notify operators and do not enforce runtime guards.
Diagnostics OFF still needs Prometheus, Alertmanager, bounded local logs and full audit; disabled
Tempo/Loki destinations must not be retried. With diagnostics ON verify content-free
`coire.api.training.*`, `coire.scheduler.training.*`, `coire.node.training.*` stage attribution.

Prometheus now packages training rules at `/etc/prometheus/rules/training.yml`;
Grafana packages `jobs.json`. Confirm the provisioned datasource UID `prometheus`, authenticated
same-origin API link routing, and `/docs/runbooks/sft-training#...` runbook URL resolution (repository
Markdown is not proof that HTTP documentation is served). Verify Alertmanager delivery and clearing
with diagnostics disabled, collector-exported metric names and bounded labels in both profiles.
The isolated collector naming/rule tests are measured in operations evidence;
scheduler lifespan wiring, real Alertmanager delivery, documentation HTTP routing,
and diagnostics-profile acceptance remain required.

## Verification and remaining release gates

Local rule validation uses the existing hardened image's `/bin/promtool`, isolated with no network,
read-only repository mount, dropped capabilities and no production service restart:

```bash
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src="$PWD",dst=/workspace,readonly --workdir /workspace --entrypoint /bin/promtool coire-prometheus:dev check rules deploy/observability/alerts/training.yaml
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777 --mount type=bind,src="$PWD",dst=/workspace,readonly --workdir /workspace --entrypoint /bin/promtool coire-prometheus:dev test rules deploy/observability/tests/training.test.yaml
```

These tests inject signals to prove thresholds, pending/firing/clearing and annotations. They are
not runtime emission, delivery, build or hardware measurements. Remaining gates include the full
single/two-node LoRA/QLoRA/DoRA train/serve matrix; three restored-state interruption trials with
fixed tolerances; all-rank/link/transfer faults; >=100 requests **per target per 15-minute phase**,
positive training updates, <=1.5-second p95 and zero swap growth; protective/cancel/lease timings;
tiny offline engine gates; complete static/contract/transaction/web regressions; production image
build/scan/SBOM and frozen wheelhouse; actual diagnostics-on stage attribution and diagnostics-off
progress/audit/alert delivery; and disable/drain/fence/backup/restore/rollback evidence. Record only
measured results, never planned thresholds as observed numbers. Feature 017 comparisons remain
unavailable; scheduled held-out loss is the 016 evaluation surface.


Administrators can open this checked-in runbook at `/docs/runbooks/sft-training`.
The web build embeds its contents and preserves heading anchors used by alerts.

Distributed loss samples are canonical from rank zero. Every authenticated participant
mailbox receipt remains recorded, including other ranks' progress and checkpoint
components; timing and memory differences between ranks must not reject their events.


Legacy `/v1/completions` accepts one string prompt, formats it as a canonical user
turn, and returns OpenAI text-completion choices in JSON or SSE. It uses the same
registry/adapter selector, variant binding, credentials, context/run limits,
placement and disconnect accounting as chat completions. Batch prompts, echo,
suffix and logprobs are unsupported and rejected; there is no base fallback.

### Gateway measurement failure diagnostics

`coire_training_workload_failures_total` reports fixed-arrival request failures by phase and closed reason: `concurrency_busy`, `completion_timeout`, `identity_mismatch`, `completion_error`, or `arrival_limit`. The jobs dashboard shows these counts; `coire.scheduler.training.workload_failure` spans and structured logs retain measurement and instance attribution. Any failure still makes the full measurement phase inconclusive. An occupied slot differs from a completion deadline; investigate that evidence before declaring a workload supported. Never subtract failed arrivals or shorten a window to approve a profile.


The internal measurement generation route uses a bounded database connection
pool with the configured database identity and credentials. Its first readonly
lookup doubles as a liveness check. An invalidated connection permits one fresh
lookup before admission; errors during admission, commit or generation are never
replayed. All user/key, registry, artifact, node and counting-hold checks remain
fresh. `coire_training_measurement_database_reconnects_total` and the jobs
panel record attempts without user or source labels. A sustained rate over
0.1/second alerts after one minute; inspect database availability and connection
logs, stop the affected measurement through its owner/admission authority, and
keep unqualified profiles out of shared admissions.

Readonly private measurement authorization and watcher checks use PostgreSQL
FOR SHARE on owner/key rows. Concurrent readers can proceed, while deactivation,
revocation, rotation and scope removal wait until those checks commit. Mutation
paths retain exclusive locks. The watcher resolves all frozen residents and
counted model/training holds in one fresh inventory query under canonical node
locks, checking exact engine, registry, artifact and node identities. No live
authority or inventory result is cached.

Repeated private gateway checks compare PostgreSQL-computed SHA-256 over both
complete fresh stored request and command documents. This avoids transferring
and decoding unchanged frozen JSON. Changed digests reload and revalidate the
documents; any midstream change refuses generation. The digest does not replace
current owner/key, registry, artifact, node or counted-hold checks.

Private stream-frame and credential rechecks open independent fresh sessions
on the same bounded measurement pool; they never share the admission session
or retry stream operations. Ordinary gateway requests retain the existing
pool. Credential liveness uses a fresh joined key/owner query, matching exact
key owner and version and refusing inactive owners or revoked keys.

Warm private routing can reuse the immutable execution principal from validated
parsing. Its first database operation freshly checks current owner/key state
under locks and complete request/command hashes before admission writes. Cold
routing keeps its readonly lookup. Either first readonly operation may reconnect
once after an invalidated connection and rollback; later reads, writes, commits
and streams are never retried. A changed stored principal is refused before
engine IO even when the submitted old routing digest matches.

The private generation route starts one disconnect watcher after body parsing.
Each stream frame checks its event; a receive failure fails closed. Completion
and cancellation cancel and await the watcher. Ordinary gateway requests keep
their existing disconnect handling. `measurement disconnect receive failed`
is a content-free warning; the measurement reports a completion failure and
remains unqualified.
