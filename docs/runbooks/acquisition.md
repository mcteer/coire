# Model acquisition operations

Model acquisition is an admin-only DBOS workflow owned by `coire-scheduler`. The API stores the
request and metadata-only inspection; scheduler stages issue idempotent work to the origin Studio
and never move weights through core.

## Inspect and retry

```bash
curl -fsS -H "Authorization: Bearer $COIRE_ADMIN_TOKEN" \
  "http://coire-core.lab:8180/api/v1/admin/acquisitions/$WORKFLOW_ID" | jq
curl -fsS -X POST -H "Authorization: Bearer $COIRE_ADMIN_TOKEN" \
  "http://coire-core.lab:8180/api/v1/admin/acquisitions/$WORKFLOW_ID/retry" | jq
```

Stages are `inspect → pull → convert → validate → replicate`. An already-MLX source records convert
as a no-op. Retry is accepted only for failed work; successful stage results remain immutable.

To stop physical work during an incident, read its node job id from the audit/trace and send an
authenticated `DELETE /node/jobs/{job_id}` to that Studio. Cancellation releases conversion
reservations; partial conversion directories are removed and partial Hub pulls remain resumable.

## Diagnose

- `gated`: accept the Hub licence using the account whose token is in that Studio's System Keychain.
- `gguf_only`: use the original safetensors repository or a pre-quantized MLX repository.
- `unsupported_architecture`: the pinned `mlx-lm` cannot load it; do not enable remote code.
- `no_fit_memory`: use a smaller/pre-quantized model; a merely busy node queues instead.
- `incomplete_visual_processor` or `missing_visual_weights`: inspect the repository's processor,
  tokenizer and safetensors inventory; no weights were transferred.
- `vision_requires_preconverted_mlx` or `unsupported_visual_architecture`: use a supported,
  already-converted MLX-VLM repository; visual conversion is not available.
- `unsupported_visual_recipe`: a preconverted visual source has no matching inspected precision
  or the requested recipe would convert it. The audited refusal transfers zero weight bytes.
  Submit the source's measured precision and group size. A supported visual source enters the
  admin acquisition workflow; it remains unpublished until the local one-image smoke passes
  and the replica checksum matches. If either check fails, inspect the acquisition job and
  cancel or retry through the admin API. To roll back, stop new visual submissions and keep
  failed variants unpublished while retaining their job evidence.
- `disk_full`: free model-store capacity; partial conversion output is removed.
- validation failure: compare smoke, perplexity, and template outcomes; files stay unpublished.
  For a visual node job, inspect `result.backend=mlx_vlm`, `smoke_failure`, and the local
  checksum manifest. The node refuses incomplete or linked processor files, missing weights,
  unsupported visual architecture, and degenerate one-image output. Visual perplexity is
  `not_comparable`; a pass includes measured `visual_input`. A failure leaves the variant
  unpublished. Stop the job through the existing acquisition cancel path and keep the admin
  visual acquisition refusal enabled until scheduler publication is validated end to end.

Use the **Coire Acquisition Jobs** dashboard for stage duration, reservations, validation, and
estimate drift. Alerts cover stuck stages, exhausted conversion retries, and >10% size drift.

## Image asset file preflight (feature 015 staging)

The Studio node has `image_asset_files` and `snapshot_image_asset` helpers for the upcoming
admin-only image acquisition workflow. The preflight requires a resolved commit, safe unique
paths, safetensors with upstream digests, and local configuration for base/control/upscale
assets. The classifier is limited to the pinned Falconsai revision and weight digest in the
feature research. Its upstream repository also contains `.pt` and `.bin` files; the snapshot
helper selects exact inert files and never transfers those pickle weights. An unsafe path or
changed classifier digest refuses the asset before transfer. Investigate the inspected file
list and pinned revision, then retry only through the admin workflow once it exists. This
helper is not yet wired into acquisition or publication; existing language acquisition remains
unchanged. Rollback removes the image helper with the node version; no model files are acquired
by this staging slice.

## Raw retention and rollback

Raw weights are removed only after validation and two matching copies unless `keep_raw=true`. Stop
new submissions before rollback. Allow or cancel active workflows, then roll API, scheduler, and
both node agents back together. Leave the additive tables in place; do not downgrade after a second
variant exists.

## Live rollout note — 2026-09-02

The production API currently reports the GLM 5.3 Flash mixed-quant registry row as failed with
`not_found: no such job`, while the origin Studio has a complete 181,944,533,258-byte,
28-file model directory and manifest. This is the pre-fix job-visibility race addressed by the
API change in `apps/coire-api/src/coire_api/registry/acquisition_executor.py` (initial node-job
404s are tolerated for 15 seconds after submission).

Roll out the API and scheduler images from the same revision before retrying that model. Then use
the admin retry endpoint and wait for the durable node job to reach `done`; do not delete or
re-download the existing complete origin files. The live core host is not currently reachable
with the available SSH identity, so this rollout remains an operator action. No model weights or
credentials are recorded here.

A 2026-09-02 read-only SSH check confirms the origin directory and manifest are still present on
`coire-edge-a` (about 170 GiB); no GLM manifest or model directory is present on `coire-edge-b`.
This is consistent with the failed visibility race and reinforces that retry must reuse the
existing origin files rather than starting a second pull.
