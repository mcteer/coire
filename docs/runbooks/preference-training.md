# Preference training operations

Feature [018](../../specs/018-preference-optimisation/spec.md) uses numeric schema
version 3 for DPO and ORPO. Existing v1/v2 SFT documents and checkpoint hashes retain
their original interpretation. Current implementation and qualification evidence
are recorded in the [execution record](../../specs/018-preference-optimisation/execution-record.md).
Full release qualification is still in progress; native component evidence alone
does not approve a deployed configuration.

## Supported configuration

The implemented matrix is one Studio, dense Llama/Qwen2 LoRA or acquired affine
4-bit/group-64 QLoRA, explicit supported linear targets, zero dropout and
`final_assistant` loss masks. DoRA, distributed preference training, unsupported
families, multimodal/tool/reasoning pairs and executable dataset plugins are
refused. Only node-owned bare MLX workers load model/tokenizer tensors. Core stores
contracts and immutable identities and coordinates authenticated work.

DPO sums response token log probabilities and uses a frozen independent reference
equal to the exact initial policy. ORPO uses response means and has no reference
model. Set the objective's beta/weight explicitly through the generated recipe
contract. `recipes/training/dpo.yaml` and `orpo.yaml` are registry-bound templates;
replace every placeholder before `coire train validate` and submission.

An optional `init_adapter` names a ready private registry adapter with an exact
base, manifest, target modules and LoRA configuration. It does not name a path.
Initialization copies its complete trainable tensors; the new optimizer starts
fresh. Live jobs and measurements retain the exact parent input. The child output
contains complete tensors and an immutable ancestry snapshot, bounded to 32
ancestors, that remains readable after parent retirement.

## Measure and admit

New preference measurements/jobs require both `COIRE_TRAINING_ENABLED` and
`COIRE_PREFERENCE_TRAINING_ENABLED`; both default off. Both nodes must advertise
v3 capability. Use the Training UI and authenticated measurement/job API in the
generated OpenAPI. Approval requires actual owned measurements, complete stop
proof and a current exact profile. Never approve a profile manually.

The fingerprint binds hardware/runtime, base, parameterization, targets,
objective/options, initial/reference identity, paired datasets, sampler/probe
implementation, sequence and batch settings. DPO's envelope includes a second
frozen base plus any reference adapter, paired activations, optimizer and peak
checkpoint/probe allocations. ORPO does not reserve a reference. Changes require
new measurements. Existing SFT profiles cannot authorize a v3 job.

Resident chat remains protected by pins and leases. Mixed training/chat requires
its own current exact measured profile. Images and training are mutually
exclusive on a Studio. Missing, stale or unhealthy samples block admission.

## Observe and stop

The durable run view shows objective loss separately from deterministic post-update
preference accuracy/margin and response token counts. Probes restore model modes,
RNG and sampler state. Declared task/judge suites reuse
[feature 017 evaluation operations](evaluations.md); an empty v3 suite creates no
obligation. Scores do not grant harness verification.

Use authenticated pause/cancel controls. Disabling admission does not stop an
owned process. Keep reconciliation active until every process has complete fenced
stop proof. Preserve counted reservations on unknown liveness. Training baseline
alerts and history remain available independently of diagnostics.

Coexistence measurements renew prepared reservations during input delivery and
the full chat baseline, then renew running trainers. Each renewal follows fresh authority,
input-binding and resource checks. A missed lease still expires on the node;
expired or stopped work cannot be renewed or started. Treat a refused transition
as inconclusive, retain its stop proof and drain its owned resident before retrying.

## Recover and serve

Resume requires a committed complete mirrored checkpoint: policy and optimizer
tensors, schedule, RNG, pair sampler, objective/options and immutable
initial/reference/runtime/input identities. Partial or mismatched copies fail.
An evaluation-owned pause retains its checkpoint pins; a later administrator
override takes precedence over automatic resume. Fresh admission follows complete
trainer/evaluation cleanup.

Extraction validates complete standalone output against the frozen recipe.
Inspect ancestry with `coire adapter lineage ADAPTER_UUID`. New adapters remain
private and unverified. Select their exact registry base@adapter target through
the authenticated gateway; there is no fallback to base. Write-capable harness
use still requires the existing exact-pair verification gate.

## Rollback

Disable new preference/training admissions. Cancel queued jobs, pause or cancel
active trainers, and drain evaluation-owned pauses before changing binaries.
Require complete physical stop and cleanup proofs; preserve holds for uncertain
processes. Drain export work using [feedback operations](feedback.md#rollback).
Keep the current node runtime and immutable artifacts until recovery receipts
are recorded. Re-pin the previous compatible control-plane images and frozen node
environment through the normal upgrade workflow. Do not downgrade a live schema
or restore weights-only checkpoints. Record old/new binary and runtime identities
and revalidate legacy SFT/evaluation history before reopening admissions.

The prior 017 node cannot parse v3 attempt, measurement or adapter-extraction
recovery records. After proving every native process stopped and all core work
terminal, preserve the complete private `training/attempts`,
`training/measurements` and `training/extractions` directories in an offline
rollback archive before selecting that binary. Freeze the owned node process
during the atomic directory move and restart it through the unchanged service
manager. Preserve the temporary legacy runtime directories separately, and restore
the original directories before selecting 018 again. Never move model, dataset
or published adapter artifact directories, and never discard unknown-liveness
records or their holds. The 018 rehearsal verified authenticated old-node health
and real gateway inference with this drained archive, then restored all records.

Older chat binaries do not understand 018 active-answer selection. During a
drained rollback, quarantine affected feedback owners through the audited admin
user mutation path until supported chat tools return; record and restore their
previous active state. Keep capture disabled for owners who opted out. Retain
the additive database schema and published export membership. An isolated seeded
downgrade test is evidence for the migration guard, not authorization to destroy
production feedback records.

Coexistence workload traffic uses the node-authenticated internal gateway
measurement endpoint through `COIRE_TRAINING_INPUT_API_URL`. It is limited to an
active, frozen measurement; callers cannot submit latency reports or substitute
owner principals. Inspect gateway metrics under the API service identity, rather
than the scheduler identity, when assessing the final profile. Earlier scheduler
proxy measurements are diagnostic and remain inconclusive.

If callback timing passes while the real gateway breaches its overhead budget,
check synchronous work on the API event loop as well as database contention.
Credential verification is worker-thread bounded to one Argon2id operation per
API process; do not increase its memory concurrency or lower hash parameters to
qualify a workload. Revocation and rotation during verification must refuse the
old credential, and ordinary live stream/action rechecks still apply.

The private measurement database pool is bounded to two connections plus two
overflow connections. Its first readonly query checks liveness and may reconnect
once after an invalidated connection and rollback. No admission write, later
query, commit or generation is replayed. Monitor
`coire_training_measurement_database_reconnects_total` and
`CoireTrainingMeasurementDatabaseReconnects`; repeated disconnects require
investigation rather than wider retry or pool limits.

Warm routing retains only immutable execution identity. Complete fresh stored
document digests, current owner/key state, registry/artifacts, node inventory and
counted holds are checked on every guard. Shared owner/key read locks still block
revocation and other credential mutations until the check commits. The private
disconnect watcher fails closed. An independent credential check starts after
request-lease commit and before output; its result gates the first chunk.

The API uses exactly pinned uvloop 0.22.1; native node environments are unchanged.
Qualification must declare its request interval, concurrency, token counts and
exact resident/training identities. A shorter diagnostic window or reduced rate
does not qualify another workload. Keep the 900-second phases, zero failures,
1.5-second chat p95 and 20-ms gateway p95 limits unchanged.


Private exact-instance admission batches the fresh-key rate/quota check and
request lease in one atomic database statement under the original locks. Rate or
quota refusal must leave no lease, in-flight increment, hold usage change or usage
settlement, and must never start inference. Do not bypass these counters or weaken
durable commits to meet the gateway budget.


If preparation status times out while retained checkpoint/artifact history grows,
inspect node admission/status latency and fresh disk-accounting timing before
deleting anything.
The current runtime calculates quota from a fresh path index; it must continue to
charge unknown retained files and preserve committed dataset/checkpoint content.
An inconclusive measurement is not an admission profile. Require a positive
fenced stop receipt before releasing its holds and rerunning the exact workload.

Repeated cold measurement-generation 404s with a valid node binding can indicate
inconsistent frozen-principal hashing in mismatched API/scheduler builds. Deploy
matching checked builds that canonicalize unordered principal fields. Do not
remove principal digest checks or disable the original administrator to work
around the refusal. Fresh live authorization still applies to every completion.

For a slow exact-profile measurement, inspect the existing
`coire.api.training.measurement.gateway` trace. Its
`coire.api.training.measurement.admission_stage` events contain only `stage` and
`duration_ms`: initial authority, node lock, fresh inventory, lease and commit.
Use these timings to locate admission cost without exposing prompts or credentials.
A short diagnostic or inconclusive measurement supplies no admission profile;
keep the workload's declared arrival interval with its accepted evidence.
