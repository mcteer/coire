# Studio image worker preparation

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
The image worker and language engines share one admission lock and count one
another's committed reservations. The node reservation ledger sees their sum.
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
