# Quickstart: Validate the Model Acquisition Pipeline

## 1. Contract and local gates

```bash
uvx --from openapi-spec-validator openapi-spec-validator \
  specs/002-acquisition-pipeline/contracts/acquisition-api.yaml
uvx --from openapi-spec-validator openapi-spec-validator \
  specs/002-acquisition-pipeline/contracts/node-acquisition-api.yaml
uv run ruff format --check && uv run ruff check
uv run mypy apps/ packages/
uv run pytest -q -m "not integration and not engine"
```

Expected: contracts, generated OpenAPI/types, migration, workflow recovery, reservation, validation,
and no-Hub-upload tests pass.

## 2. Composed pipeline

```bash
COIRE_INTEGRATION=1 uv run pytest -q -m integration \
  tests/integration/test_acquisition_pipeline.py
```

The composed suite uses public ≤100 MB Hugging Face fixtures for raw safetensors, already-MLX, and
unsupported-architecture paths. It asserts two verified copies, zero-byte early refusal, duplicate
attachment, conversion from an existing verified variant without a second pull, and both scheduler
and node restart recovery. GGUF, gated, oversized, disk-full, and validation-failure branches use
deterministic local fixtures in the unit/contract suites so the release gate does not depend on a
third party changing those failure repositories.

## 3. Live raw conversion

On core, add a ≤1 GB raw safetensors fixture with a 4-bit affine recipe and `keep_raw=false`. Record
the workflow ID and observe `inspect → pull → convert → validate → replicate → done`:

```bash
scripts/coire model add <org/raw-test-repo> \
  '{"variant":{"name":"4bit-g64","bits":4,"group_size":64,"mode":"affine"}}'
scripts/coire model job "$MODEL_ID" | jq
```

Expected: one Studio holds a conversion reservation, no swap begins, both variant copies share one
manifest, validation is true, and raw files are absent only after replica verification.

## 4. Validation and comparison

With `keep_raw=true`, request a second 6-bit variant. Confirm Hub transfer counters do not change,
then compare byte size, perplexity, smoke, and template results. Mark the validated 4-bit variant
default/published; `/v1/models` lists the base registry UUID once and routes it to that variant.

## 5. Failure and recovery

- Restart `coire-scheduler` after convert begins: completed stages do not rerun.
- Restart the origin agent during convert: partial output is removed and convert restarts at its
  stage boundary.
- Exhaust current memory with a loaded model: workflow reports `waiting_for_capacity` and occupants,
  then continues after unload.
- Fill a bounded test destination: conversion fails `disk_full`, releases its reservation, and no
  variant path is published.
- Force perplexity outside tolerance: files remain, `validated=false`, publication is refused.

## 6. Observability and audit

Use the Jobs dashboard and trace links to identify queue, conversion, validation, and replication
time. Verify alerts for a stuck stage and forced conversion failure. Audit must contain the admin
request and every stage transition without tokens, prompts, or local paths in public responses.

## 7. Rollback

Stop new acquisition submissions, allow/cancel active workflows, restore the previous scheduler/API/
node versions together, and leave additive tables intact. Existing default variants remain usable
through the legacy model projection. Do not downgrade the migration after creating a second variant.
