# Image generation operations

Feature 015 is under construction. `COIRE_IMAGE_ENABLED` defaults to `false`; keep it off
until the unchecked tasks and acceptance matrix in
[`specs/015-image-generation/tasks.md`](../../specs/015-image-generation/tasks.md) and
[`quickstart.md`](../../specs/015-image-generation/quickstart.md) pass. An API job receipt or
a simulated worker test does not prove that a Studio can generate, cancel, clean up and
publish a real batch.

## Acquire and validate an image asset

An active human admin submits `POST /api/v1/admin/image-assets` with a Hub `repo_id`,
one of `image_model`, `image_lora`, `control_model`, `upscale_model` or
`image_classifier`, and `accepted_license_id` exactly matching the inspected model
card. Review each model licence separately before submission. The API records the
resolved commit, selected safe file inventory and licence in the registry and audit
row. Only the origin Studio pulls; it selects inert files, hashes them against Hub
safetensors digests, and sends the verified manifest to the replica. Both copy
manifests must match the inspected inventory before validation starts.

The `VERIFY_REPLICA` acquisition stage now holds a measured Studio reservation
and invokes an offline `image_validate` job on each copy. A base model must produce
non-degenerate pixels from a neutral prompt; the narrow measured capability and
thumbnail digest are recorded with both results before either reservation is
released. The validator samples the process's physical footprint during both
native smokes, including Metal memory on macOS, and refuses a transient peak
above the held bytes. The gate compares the **absolute process peak** with the
held bytes, while recording absolute RSS/physical peaks and the physical
baseline delta. If a validation
job is still running, leave the
reservation held and inspect its exact job ID, process identity, local manifest and
`CoireImageValidationFailures` alert. Auxiliary mode-specific validation is still
unavailable and those assets remain unpublished. Do not set the image admission
flag until T033 and the remaining real Studio acceptance pass.
Base acquisition reserves the larger of twice the selected file bytes or three
times the weight bytes, plus 16 GiB of native runtime headroom, on **each**
Studio. A Studio below that estimate refuses intake before pulling. The smoke
still rejects a base whose measured peak exceeds its actual reservation.

Gallery cards show a safe classifier diagnostic when tagging is unavailable or
fails. An `unknown` tag does not mean the pixels were classified as normal;
keep those outputs private while diagnosing the pinned Studio classifier.
The worker uses only an exact locally verified classifier copy. It classifies
each generated PNG after generation within the existing image worker hold;
`COIRE_IMAGE_CLASSIFIER_MEMORY_BYTES` caps the classifier child to 1 GiB by
default. New image placements add that allowance to the model's held memory
estimate, including when the classifier copy is temporarily unavailable. If
the copy is absent or corrupt, publication records `unknown` with
an unavailable diagnostic. If the held worker has insufficient measured headroom,
the classifier does not start and records `classifier_memory`.
Both fallback paths count as failed classifier stages for the
`CoireImageClassifierFailures` alert.

The acquiring admin must record each asset's licence review and accepted licence
ID before the acquisition request. A model with an unverified copy, missing local
component, failed offline smoke, or incomplete auxiliary validation remains
unpublished; retry the exact acquisition job after fixing the source inventory.
Inspect the model and acquisition job through the admin registry API and the
`CoireImageValidationFailures` alert. Never repair a missing component by asking
the generation worker to fetch it.

## Native worker readiness after restart

The node re-adopts an image child only by its exact PID, creation time, private
launch configuration and verified model manifest. It resets historical `ready`
to `starting` while retaining the full resident hold. Authenticated loopback
health must prove the same identity, port and reserved bytes before dispatch
can resume. A missing or mismatched reply is not permission to clear a hold or
start another child. During termination, transient identity-inspection failure
keeps the hold while the node continues waiting for death within its bounded
stop window; an unknown identity is never signalled.

The native loader evaluates all model parameters on their creating thread
before background generation. `COIRE_IMAGE_PROMPT_CACHE_MAX_BYTES` is carried
in the node's private launch configuration and enforced by the native encoder
LRU; setting it to zero disables retention without disabling generation. Older
private launch records default to 256 MiB when read by the new node revision.
Drain workers before rolling back to a revision that rejects this additional
private configuration field. MLX stream ownership is thread-local: unevaluated
weights must not cross into the generation thread. The local tiny-engine gate
exercises the actual bootstrap child, authenticated generation before and after
supervisor reconstruction, identical PID and pixels, and confirmed bounded stop.
This is local mechanical evidence, not full-model Studio acceptance.

## Inspect and stop current work

An authenticated owner can use the Images page to see job state, progress and private
outputs. `GET /api/v1/images` lists their jobs; `GET /api/v1/images/{job_id}` reads one,
and `GET /api/v1/images/{job_id}/events` replays owner-scoped events. The page's **Stop**
control calls `DELETE /api/v1/images/{job_id}`. A 202 response means cancellation was
requested; continue observing until a terminal event. If the worker or node cannot prove
termination, the job and its reservations remain held for reconciliation. Do not remove
its files or clear its holds by hand.
The scheduler persists the exact node attempt journal before loading the
worker. A cancellation arriving during model load can therefore cancel that
queued node attempt without starting generation. A replayed terminal node
journal prevents the scheduler from issuing another worker start.
If a placed job's Studio journal or worker reply disappears, the scheduler keeps the
execution lease and storage hold. The `CoireImageObservationFailures` alert is expected;
inspect the node and reconcile exact process identity and scratch before terminal failure
or release. A 404 alone does not prove that a worker process stopped.
When the worker itself reports `failed`, the node removes its exact attempt's
output and input scratch first. A later status request repairs an older failed
journal with unacknowledged cleanup. Core records the terminal error and releases
the execution lease and output hold only after `scratch_cleaned=true` and core
transfer staging has been removed. Watch
`coire_image_observation_total{outcome="failed_cleaned"}` and the image node stage
failure alert. If cleanup fails, keep the lease and hold; inspect the exact
scratch namespace for unexpected files or links before retrying observation.
If a live user, key or entitlement is revoked, the scheduler requests fenced cancellation
on its next observation. Watch `coire_image_observation_total{outcome="revoked"}`, the
`CoireImageAuthorizationRevocations` alert and the job's terminal event; the alert means
the request was recorded, not that node cleanup has been proved.

The admin Activity page and `GET /api/v1/admin/image-jobs` show recent image job
status without prompts or blob paths. A human admin can inspect an exact job and
use **Kill** (`DELETE /api/v1/admin/image-jobs/{job_id}`); each inspection and
cancel mutation is audited. A kill response is an intent until the node proves
termination and scratch cleanup. For an idle image worker, the Activity page's
**Unload** action calls `DELETE /api/v1/admin/image-workers/{instance_id}`. The
API refuses an active job or unreleased image execution lease, marks the
instance draining before its exact node stop command, and records both request
and confirmed completion. If node stop is uncertain, leave the instance in
`draining`, inspect the exact process identity on the Studio, and retry the
same admin command after reconciling node health. New image placement on that
Studio waits while an image worker is draining. Use node and scheduler logs,
image metrics and job/lease records for investigation. The Images dashboard and
image alerts show the corresponding fenced outcomes.
A worker marked failed can use the same unload path once its jobs and leases
are terminal; the API still releases memory only after the node proves exact
process death and zero reserved bytes.
The scheduler also scans unpinned image worker reservations in bounded batches.
After `COIRE_IMAGE_WORKER_IDLE_TTL_S` without a job or image execution lease,
it marks the instance draining, requests the same exact node stop, and releases
the memory hold only on the node's zero-byte stop proof. Check
`coire_image_worker_idle_unloads_total{outcome="confirmed"}` and the
`coire.scheduler.image.idle_unload` span. An `uncertain` outcome leaves the
draining instance and reservation held and raises `CoireImageIdleUnloadUncertain`;
inspect the node and retry the admin
Unload action after reconciling process identity. Pin an active worker through
the admin ledger reservation PATCH to exempt it from idle unload. A worker
already draining cannot be pinned; complete its stop reconciliation first.
If the agent restarted after the worker died, the same unload command can
reconcile the private process record. It removes that record only when the
recorded PID and creation time prove the child is gone. An unreadable record,
uncertain PID or changed instance ID keeps the hold and needs investigation.
Image worker memory is held in the shared placement ledger until exact node
stop proof returns zero reserved bytes. A missing reply keeps the reservation
held, including after a restart. If a Studio agent refuses startup because its
`reservations.json` journal is malformed or unreadable, preserve that file,
inspect running engine and image worker identities, and restore the journal
from a verified backup before restarting the agent. An empty replacement could
erase a live conversion hold and over-admit memory.

Personal API keys need `images` for standard generation and reads, plus
`images:explicit` for explicit work. The owning human also needs live entitlement;
an admin role alone does not bypass an owner's output policy. Admin mutations
require a live human admin and are audited. If an owner or key loses access while
a job runs, inspect the cancellation intent and wait for a terminal node proof
before releasing the reservation.

## Output retention policy

By default, published outputs remain until the owner deletes them; storage quota
still applies. Before generating, the Images form displays the API's current
owner quota, pending/daily output caps, upload/output byte ceilings and retention
policy. Missing policy blocks submission rather than guessing. Authenticated
`GET /api/v1/images/models` returns the same `limits` when admission is disabled.

An operator can opt into `COIRE_IMAGE_OUTPUT_RETENTION_HOURS` (1–8,760 hours).
Blank/unset means no automatic output expiry. The period starts at successful
publication, not upload/staging; enabling or shortening it also applies to
existing outputs. Review the deletion consequences before restarting the API.
Apply migration `0030_image_output_retention` before enabling this policy. Its
partial publication-time index keeps the ordered sweep off unrelated history;
as with other transactional schema changes, drain image writes for migration.
The 30-second maintenance loop locks and tombstones at most 25 expired published
outputs per pass, skipping locked rows. Each tombstone requires the system audit
`image.output.expire`; an audit/database failure rolls back the batch. Tombstones
immediately deny new reads/grants, while the existing physical purge releases
owner/global stored-byte quota only after removal is proven. Inputs and SSE event
retention are independent and unchanged. Maintenance remains active with image
admission disabled. Inspect `coire_image_purges` failure counters and the existing
purge alert; bounded maintenance spans contain sweep names, never image content.

Local PostgreSQL tests prove row-lock skipping, audit rollback and repeat-pass
idempotence. They are not evidence of full-model/operator retention acceptance.

## Access and stored data

Use the owner gallery to inspect or delete published outputs. Downloads request a short
lived owner grant and redeem it in `X-Coire-Image-Grant`; keep that header and any returned
fragment out of logs and copied URLs. Deleted output metadata becomes unreadable
immediately, while the bounded cleanup pass unlinks bytes before releasing quota. The
same physical-deletion rule applies to owner image inputs. Recipe uploads accept PNG
files up to 64 MiB and extract metadata without decoding pixels. Init, mask and
control uploads accept still images up to 10 MiB; the isolated file worker normalizes
them to private PNGs, applying EXIF orientation and preserving white-edits/black-keeps
mask meaning. Processing holds space for both original and normalized bytes. Poll
`GET /api/v1/image-inputs/{input_id}` until `ready`; a failed input retains its hold
until cleanup removes both files. Deletion hides the input immediately and credits
quota only after physical cleanup. Active job references must be cancelled and drained
before deleting an input.

An expired grant cannot be redeemed. Request a fresh owner grant with current
credentials; the web gallery retries a failed expired grant once. A 401 means the
session or key expired, a 403 means live scope or entitlement no longer permits
access, and a 404 may mean the output was deleted or never belonged to the caller.
The API enforces per-owner and global pending-job limits, a daily output allowance,
per-owner/global stored-byte caps, and a disk safety floor before issuing a receipt.
The configured values and hard maxima are in
[`deploy/compose/README.md`](../../deploy/compose/README.md). Quota holds remain
until physical deletion or a fenced terminal outcome proves release. Inspect the
image quota and job rows before changing caps or retrying a failed cleanup.
Published outputs remain private until the owner deletes them; deletion starts a
bounded physical purge and quota is credited only after unlink succeeds. Deleted
and failed inputs have a 24-hour purge deadline. Job event history is capped at
24 hours, so inspect the durable job snapshot after an event cursor expires.

Classifier output is a private `normal`, `explicit`, or `unknown` tag. Unknown means
the pinned offline Studio CPU stage could not classify; it remains visible only
through owner-authorized private routes and is never shareable. Policy-explicit output remains explicit
even if classification fails. Inspect the classifier revision, processor digest,
threshold and safe error in the output record and the `CoireImageClassifierFailures`
alert. The transfer receipt preserves the Studio classification and publication
stores its tag, score and diagnostic. Standard output falls back to `unknown`
when the classifier is unavailable. See the
[classification runbook](image-classification.md) for the stage and rollback steps.

On a Studio, a fenced advanced attempt reserves its input manifest before accepting
normalized PNG bytes. The node checks the bound job, attempt, purpose, digest and
dimensions and stores them under private `image-input-scratch`; queued cancellation
removes that scratch. Admission retains every ready owner input in stable lock
order, checking its purpose, digest and dimensions before incrementing any
reference. Dispatch builds a separate exact manifest for each init, mask or
control input and transfers each normalized PNG before the worker starts.
Image-to-image verifies the init digest again inside the worker. Terminal
publication, cancellation and clean worker failure release the active input
references. Deleting an input
used by an active job requests fenced cancellation and tombstones the input;
the purge waits for the node's stop and cleanup acknowledgment. Fill, control, LoRA and upscale
execution remain under T056; full end-to-end input acceptance remains under T054.

The `coire-blobs` volume is API-only. Back up and restore it together with the Postgres
rows that describe image inputs, outputs, receipts and quota holds. Restoring only one
side can leave inaccessible outputs or unreconciled storage reservations. Confirm that
pending transfer and deletion sweeps finish before any rollback. Do not manually unlink
files from a running job's private namespace.

## Rollback and diagnostics

Disable new admission with `COIRE_IMAGE_ENABLED=false` through the documented compose
configuration. Existing jobs still need cancellation or completion reconciliation;
turning the flag off does not prove a worker stopped. Drain them and verify node scratch
cleanup acknowledgments and core receipts before rolling back API, scheduler or node
versions. The image schema migrations include guarded downgrades; use the migration tests
and required drain preconditions before applying one.

Lean observability retains Prometheus metrics, alerts and audit rows. Historical trace
and centralized log search require the diagnostics profile. Image telemetry uses bounded
operation/outcome labels and must never include prompts, images, grant tokens or owner IDs
as metric labels. The parent execution record notes which local checks passed and which
real engine, browser, migration, image-scan and Studio gates remain open.

The operator's repeatable mixed-workload probe is
`uv run python tests/benchmarks/image_chat.py --help`. Supply a user bearer file,
an admin bearer file for node thermal/memory readings, a local Prometheus URL,
the two registry model IDs and the target Studio. It runs for 900 seconds by
default, pins chat to that Studio and verifies the image job's recorded runtime
fingerprint against the target. Write the content-free JSON report outside the
repository, then attach its measurements to the execution record. The node
memory-used reading is an aggregate footprint proxy; approval still requires
the same-node process and no-swap checks in T084.

After reviewing that 15-minute report, a live human admin may submit its
content-free measurements to `POST /api/v1/admin/image-coexistence-profiles`.
The API checks the ready Studio, published image base, validated published chat
variants, measured image bounds, first-token p95 <=1.5 seconds, gateway p95
<=20 ms, completed image count, progress, swap and thermal state before
auditing an approved profile. The report must be completed within the past
day and expire within seven days. `hardware_fingerprint` is SHA-256 of compact
JSON `[node_name, memory_total_bytes, gpu_cores]`; `runtime_fingerprint` is
SHA-256 of compact JSON `[agent_version, "mflux-0.20.0"]`. Admission recomputes
both from the current node row, so a registered hardware or agent change
refuses the previously measured mix. A fresh (<=30 seconds) serious or critical
Studio thermal sample blocks new placement, including a pinned node, and
requests audited fenced cancellation for an active image job. The 15-second
coexistence monitor also withdraws current approvals on that sample and
requests cancellation before querying chat latency. Inspect
`CoireImageThermalCancellation`, `coire_image_observation_total{outcome="thermal_alarm"}`
and `coire_image_latency_monitor_total{outcome="thermal_alarm"}` plus the
`coire.scheduler.image.thermal_check` span. The scheduler also queries
its internal Prometheus service every 15 seconds for each node with an approved
profile. A five-minute same-node first-token p95 above 1.5 seconds
atomically invalidates those approvals and requests audited fenced cancellation
for active image jobs on that node. Inspect `coire_image_latency_monitor_total`,
`CoireImageCoexistenceInvalidated` and the `coire.scheduler.image.latency_monitor`
span. No chat sample leaves the measured approval in place. An unavailable or
malformed monitoring response withdraws approvals and requests the same fenced
stop, then raises `CoireImageLatencyMonitorUnavailable` for investigation.
An admin can invalidate one approved profile with
`DELETE /api/v1/admin/image-coexistence-profiles/{profile_id}`; the audited
invalidation takes effect on the next placement check. An already running
image job needs the normal fenced cancel path, since invalidating a profile
does not release or kill a live worker by itself.

Chat request lease insertion shares the transaction-scoped node admission lock with
image dispatch and eviction. It refreshes and locks the reservation after admission
before requiring a HELD hold; a reservation released while a request waits cannot
be leased from stale session state. Sharded requests acquire the complete node set
in canonical order before inserting any rank lease. These locks are released by
the short database transaction, not held throughout chat decoding. Request leases
continue to protect the ongoing request through their existing heartbeat/release
path. A request stops if renewal finds an expired lease or cannot reach the
database; the gateway attempts normal lease release in either case. Inspect
`coire_gateway_lease_losses_total` by `reason` and the
`CoireGatewayLeaseLoss` alert in the Chat dashboard, then check database health
and the node's current reservation before retrying. Do not force-release a
resident hold to clear this alert. For local concurrency verification, use only
an explicitly disposable
localhost PostgreSQL DSN and run `test_image_accelerator_admission.py`, the integration
case in `test_image_admission.py`, and the migration/quota race in
`test_image_persistence.py` under `apps/coire-api/tests/unit/`. Those tests do not
replace the measured coexistence profile or the full operator matrix.
