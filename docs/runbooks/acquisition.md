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
- `disk_full`: free model-store capacity; partial conversion output is removed.
- validation failure: compare smoke, perplexity, and template outcomes; files stay unpublished.

Use the **Coire Acquisition Jobs** dashboard for stage duration, reservations, validation, and
estimate drift. Alerts cover stuck stages, exhausted conversion retries, and >10% size drift.

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
