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
