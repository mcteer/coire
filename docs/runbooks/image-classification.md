# Image classification staging

Image admission remains disabled. The Studio node now has a local-only CPU classifier
stage for generated PNGs. The worker has not yet wired it into image job execution.

The stage starts a child with the node's versioned Python, a stripped environment,
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and no Hub token. It verifies the pinned
Falconsai safetensors size and SHA-256, loads only local files with remote code off,
and runs tensors on CPU. The parent node kills it after at most 10 seconds or when
its RSS exceeds the caller's measured reservation. A failed stage yields an owner-private
`unknown` tag and a safe code; policy-explicit output stays explicit even on failure.
It never receives a prompt and never decides whether an entitled job may run.

Inspect `coire_image_node_stages_total{stage="classify"}` and
`coire_image_node_stage_seconds{stage="classify"}` for failure and latency. Check the
stored classifier revision, processor digest, threshold and safe code on an output
record once worker integration exists. Do not log the PNG, prompt, or model path.
If the stage repeatedly times out, keep output private and investigate the measured
reservation and pinned asset on the Studio; do not fall back to remote classification.
Rollback the node version or disable image admission while preserving output tags and
audit history. Real-model quality and runtime evidence are still required before merge.
