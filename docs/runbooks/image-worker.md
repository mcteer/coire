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
