# Studio image worker preparation

The scheduler now observes an already placed `reserving` or `running` image
attempt through the node's durable journal after restart. It requires the
selected node, exact attempt/fence/instance and an active execution lease
before recording a started or progress event. Progress events are limited to
four per second; a transfer-ready journal moves the core job to `transferring`
for the separate receipt workflow. An absent or mismatched journal raises an
error and retains the capacity hold; observation never sends a new start
command. Inspect `coire_image_observation_total` and the
`CoireImageObservationFailures` alert, then compare the core job and node
journal before repairing a placement. The public submit route remains closed
until lease issuance, whole-batch publication and failure recovery are complete.

The public image submit route remains closed. The node-side image worker load
contract now carries a registry slug, manifest digest, runtime version and
reservation. Before any future mflux launch, `verify_image_copy` checks the
Studio Store copy, its canonical manifest and every file digest. It refuses
symlinks, special files, unsafe asset suffixes, extra files and a runtime other
than `mflux-0.20.0`. It never downloads missing files.

For a load refusal, inspect the admin acquisition and replica manifests on
both Studios using the existing model-copy tools. Repair through the admin
acquisition pipeline and rerun verification; do not edit the copy or its
manifest manually. The native mflux installation is in draft PR #31 and must
land before a live worker can start. To stop image loading, keep
`COIRE_IMAGE_ENABLED=false`; do not remove model copies while an engine is
active. No real Studio or engine was run by this slice.

The fixed Turbo pipeline now requires `HF_HUB_OFFLINE=1` and
`TRANSFORMERS_OFFLINE=1`, with no Hub token in its process environment, before
it imports mflux. It loads the preflight-verified local directory with the
pinned Z-Image Turbo configuration. A plain txt2img job is refused if it has
nonzero guidance, a negative prompt, LoRA, input, control or upscale settings.
Each progress callback evaluates the MLX latents before reporting a completed
step; it carries only output index and step counts. The node worker still needs
to own the process, deadline, cancellation and output transfer before the
submit route can open. To diagnose a pipeline refusal, inspect the safe worker
error code and the resolved model/runtime manifest, then re-acquire a bad copy
through the admin path. Do not use the worker process as a Hub downloader.

Generated RGB frames are now serialized to exclusive 0600 PNGs in node-owned
scratch. The writer embeds one uncompressed `coire.image` iTXt recipe with
exact effective settings, output index, seed and pixel digest. It returns the
file size and SHA-256 for the later transfer receipt and removes a partial
file after a write failure or the 64 MiB bound. It copies raw pixels into a
fresh PIL image before encoding so upstream ICC/EXIF fields are not carried
forward. A pre-existing destination is
never overwritten. To diagnose a failed recipe transfer, compare the file
digest and embedded recipe with the durable resolved job settings; discard
the scratch output and retry under a new fenced attempt if they differ.

The in-process job executor accepts only a run bound to the resident instance,
model manifest and runtime. It derives `<job ULID>-<attempt>-<fence>/` under a
node-owned 0700 scratch root and refuses a replay that would reuse the same
directory. Each successful output is `<index>.png` with its canonical recipe.
Intermediate step reports are throttled to 4 Hz; each output's final step is
reported. Expired deadlines and write errors fail the attempt and remove its
scratch directory. Process-level TERM/KILL, durable re-adoption and core
transfer still need to wrap this executor before public admission opens.

The worker control app is served on `127.0.0.1` with no docs endpoint. Every
route requires a strong per-worker bearer; `/health` reports the worker's
instance, PID, create time, port and reservation. A typed `PUT /job` returns
202 while generation runs in a background thread. Replays with the same full
request return current status; changed requests or overlapping attempts return
409. `/status` and `/cancel` require the exact job, attempt and fence. Cancel
remains in `running` until the worker stops; the node's future supervisor must
TERM/KILL it when a callback cannot run. Generated status returns only bounded
size, PNG digest and recipe digest, never a path or prompt. The in-memory
control keeps at most 32 attempt statuses, so the future node supervisor must
drain and unload before that bound is reached.

The native worker entrypoint is
`python -m coire_node.image_runtime.bootstrap <private-config-file>`. The
future node launcher writes a strict `ImageWorkerProcessConfig` JSON file and
per-worker secret as owner-only 0600 regular files. Bootstrap refuses larger
than 16 KiB configuration, symlinks, wrong ownership or mode, and weak
secrets. It then removes Hub tokens, sets `HF_HUB_OFFLINE=1` and
`TRANSFORMERS_OFFLINE=1`, verifies the local Store copy and loads mflux before
serving authenticated control on 127.0.0.1. A failed bootstrap prints only a
safe error; inspect the node load status and the admin-acquired manifest to
diagnose it. Do not place credentials in the launch JSON or command line.

The node launch supervisor now verifies the exact Store copy and configured
memory budget, including reservations supplied by its caller, before reserving
the dedicated loopback port. It writes the 0600 token
and launch JSON under `node_state_dir/image-workers/<instance_id>/` (0700),
spawns the versioned Python process with explicit argv and no Hub credential,
then atomically persists `worker.json` with PID, process create time, port and
reservation before returning `starting`. An identical load replays that
status; a different load or stale on-disk record is refused. On a launch or
record-write failure it kills the child and releases the reservation. Do not
delete a stale record manually while its PID/create-time pair might still be
alive.

Readiness now requires authenticated `GET /health` on the dedicated loopback
port and an exact match of instance, backend, PID, create time, port and
reservation. The supervisor checks the live bootstrap command before and
after the probe and atomically persists `ready`; an open socket or forged
health response leaves the worker `starting`. After a node-agent restart,
re-adoption reads the owner-only record, config and token, rechecks the local
manifest and accepts only that same live process. If the record is corrupt,
the process has changed or health is unreachable, keep the record and the
memory hold for reconciliation; do not start a replacement blindly.

An admin/shutdown unload now sends TERM only to the exact resident
PID/create-time/bootstrap process group. If it remains alive, the node sends
KILL before the five-second grace ends. It removes the private token, launch
file and durable worker record, and releases the memory hold only after death
is confirmed. Generated scratch remains until matching core receipts and the
job cleanup path remove it. A reused PID receives no signal. If process
inspection, signalling or state cleanup is uncertain, the node reports a safe
failure and retains the reservation and record; inspect the PID/create-time
pair and reconcile before attempting a replacement.

The node control listeners now expose authenticated `PUT /node/images/worker`,
`GET /node/images/worker/{instance_id}` and
`DELETE /node/images/worker/{instance_id}`. The data listener does not mount
these routes. `GET` probes the worker's private health endpoint before
promoting `starting` to `ready`; the loopback token never leaves coire-node.
The image worker, language engines and acquisition reservation ledger share
one admission lock. Each new load counts the other two live commitments, so
an acquisition hold cannot race or be ignored by a model load. Inspect the
node memory commitments and acquisition reservations together when a load
waits for budget.
`/node/health` reports the sum of language engines, the resident image worker,
and acquisition holds in `memory_committed_bytes`; these are disjoint local
holders. If any source cannot be read, the health sample reports the full node
budget instead of an unsafe zero. Compare that field with the core ledger and
the worker's exact process identity before clearing any apparent drift. The
`image_worker_resident_bytes` health field reports that live process's measured
physical footprint, including Metal memory on macOS. If an image reservation
exists but the footprint is unavailable, core leaves measured residency
unknown. It also refuses to invent a zero for an unmeasured live language
engine. Inspect the worker journal and the `coire_placement_ledger_drift_ratio`
panel plus `CoireImageResidencyMeasurementUnavailable` alert before reconciling
a reservation. Core image and chat placement subtract any measured
model-plus-image footprint above the matching reservations once from available
memory. A live image hold with unknown measured residency waits for a new
health sample before either kind of load is admitted.
On agent restart, image re-adoption runs before listeners bind. An uncertain
record holds the full image budget; inspect the process and private record
before reconciliation. Roll back the node wheel after an exact-instance
unload and confirmed process exit.

The node image job journal stores one 0600 JSON record per ULID under
`node_state_dir/image-jobs/` (0700). It records the exact resolved request and
last safe job status before worker dispatch. After an agent restart, read this
journal to reconcile the existing attempt; do not submit a new fence or rerun
denoising merely because the worker status is temporarily unavailable. A
corrupt or unexpectedly public record is left untouched and blocks replacement.
Inspect its ownership, mode and bounded JSON while the node is stopped, then
reconcile the matching scheduler attempt before any repair. Never delete an
uncertain record to make admission succeed.
Journal writes emit the `coire.node.image.journal` span and the fixed-label
`coire_image_node_stage_total{stage="journal"}` counter; a failure means the
scheduler must retain the attempt for reconciliation.

The authenticated control route `PUT /node/images/jobs/{job_id}` now accepts
only a fixed, local txt2img request bound to the ready resident image worker.
It writes `queued` then `reserving` to the private journal before the single
loopback `PUT /job`. A repeat with the identical body returns the recorded
status and never sends that command again. If the worker call times out or the
reply cannot be verified, the node returns a safe 503 and leaves `reserving`
for scheduler reconciliation. Query the exact worker attempt and journal;
do not manually replay the worker command or delete the record. Node status
polling, cancellation, transfer and publication are separate follow-up paths,
so public image submission remains disabled.

The authenticated `GET /node/images/jobs/{job_id}?attempt=<n>&fence=<n>`
observes the same journaled attempt. For active work it posts the exact binding
to the private worker `/status` route, checks its identity and advances only
valid state and aggregate progress. A complete generated batch becomes
`transferring`; terminal journal states remain readable after worker exit.
An unreachable or mismatched worker returns 503 and preserves the last journal
record. Check the `coire.node.image.status` span and
`coire_image_node_stage_total{stage="status"}` before reconciling; a status
read never starts generation.

The authenticated `DELETE /node/images/jobs/{job_id}` writes `cancelling`
for the exact attempt/fence before contacting a running worker. It first asks
the private worker to cancel; if the worker does not confirm promptly, the
node supervisor sends TERM and then KILL only to the verified PID/create-time
process group. It marks `cancelled` after a matching worker response or
confirmed process death and safe removal of the exact attempt's private PNG
scratch. A cancelled journal from an earlier worker revision is repaired on
the next exact status or cancel retry only after that cleanup. An uncertain
process or failed cleanup leaves the
journal in `cancelling` and keeps its memory reservation. Inspect the
`coire.node.image.cancel` span and fixed `stage="cancel"` metric; do not
delete the private record or publish scratch outputs while cancellation is
uncertain. A queued job with no dispatch can cancel locally.

The authenticated `POST /node/images/jobs/{job_id}/cleanup` accepts a complete
set of core transfer receipts for the same node, attempt and fence. Before
deleting anything, the node checks each private 0600 PNG's size, file digest,
canonical recipe digest and resolved settings without decoding pixels, then
persists the receipts in its journal. It unlinks the exact generated files,
fsyncs the scratch directory, and marks `succeeded` only after deletion. A
restart after unlink but before the final journal write resumes cleanup from
those persisted receipts. A mismatched receipt or unexpected scratch file
leaves the job in `transferring` for reconciliation. Do not report a core
image job as published until this cleanup receipt is durably acknowledged.

For a generated batch, the node journal records path-free output sizes and
digests. The scheduler persists a short-lived grant for each index and sends
them in one authenticated `POST /node/images/jobs/{job_id}/transfer` command.
The node checks every local 0600 PNG and its recipe against the journal, then
streams each file to the configured core ingress. Core verifies the matching
node credential, grant, attempt/fence, size, SHA-256 and embedded recipe before
issuing a receipt. The node deletes scratch only after all receipts are valid.
If an upload, receipt or cleanup acknowledgment is uncertain, inspect the
`coire.node.image.transfer` and `coire.api.image.transfer` spans and fixed
transfer counters; renew grants for the same attempt and retry transfer. Never
rerun denoising to resolve an uncertain upload.

The scheduler rescans `transferring` jobs whose cleanup is pending and starts
the same DBOS transfer workflow ID after a restart. It first reads the Studio
journal, then renews grants for that same attempt; it never sends a new
generation command. A Studio record already marked `succeeded` is reconciled
against the core's stored receipts. Only exact receipt agreement records
`cleanup_state=cleaned` and `receipt_state=complete`. A failed recovery increments
`coire_image_transfer_recovery_total{outcome="failed"}` and alerts; inspect
the `coire.scheduler.image.transfer` span and both private journals before
retrying. The image is still not public until the later publication gate runs.

The owner `DELETE /api/v1/images/{job_id}` commits a cancellation intent with
an audit row. An unplaced queued job releases its quota hold and becomes
`cancelled` in that transaction. A fenced job returns `cancelling` and keeps
its capacity hold. The scheduler retries the same DBOS cancel workflow ID,
sends the exact attempt and fence to the node, then checks `scratch_cleaned`.
It removes core transfer staging with no-follow directory handles, releases
the execution lease and quota hold, and writes the terminal event only after
those steps succeed. A missing or mismatched lease, unsafe staging file, node
503, or uncertain process leaves the job in `cancelling` with the hold intact.
Inspect `coire.scheduler.image.cancel`, `coire_image_cancellation_recovery_total`
and `CoireImageCancellationRecoveryFailures`. Reconcile the node journal and
core staging for that attempt; do not submit a new generation attempt to clear
the error. Keep `COIRE_IMAGE_ENABLED=false` to stop new admission while
allowing pending cancellation and cleanup to finish.

# Worker attempt retention

The resident loopback worker retains at most 32 fenced attempt statuses. When
that limit is reached, it reclaims generated attempts only after the node has
removed their exact private scratch directories during acknowledged cleanup.
An uncleared attempt stays available for retry and inspection. If the worker
reports busy after 32 jobs, inspect `scratch_cleaned` in the node journal and
the matching `image-scratch/<job>-<attempt>-<fence>` directory; resolve cleanup
before unloading the exact worker through the authenticated node API.

# Idle unload across node restarts

The node now persists the most recent exact `worker_stopped` proof in its
private `image-workers/last-stopped.json`. If the scheduler times out after the
worker has stopped, it can retry the same unload after a node-agent restart.
The node returns that proof only for the matching instance ID. A missing proof
keeps the core reservation draining for operator reconciliation; never release
the hold from an absent process record alone.
