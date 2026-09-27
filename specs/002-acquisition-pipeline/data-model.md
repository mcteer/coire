# Data Model: Model Acquisition Pipeline

## ModelVariant

One servable representation of a base registry model.

- `id: UUID`, immutable registry identifier
- `model_id: UUID`, owning base model
- `name: str`, unique within the base model
- `slug: str`, registry-generated node-store key
- `source_revision: str`, immutable Hub commit
- `precision: bf16|fp16|4bit|6bit|8bit|mixed`
- `bits: int?`, `group_size: int?`, `mode: enum?`, `mixed_recipe: enum?`
- `byte_size: int`, `memory_estimate_bytes: int`, `estimate_delta_bytes: int?`
- `state: requested|inspecting|queued|pulling|converting|validating|replicating|ready|failed`
- `validated: bool`, `published: bool`, `is_default: bool`
- `raw_retained: bool`, timestamps

Constraints: `(model_id, name)` and `slug` are unique; exactly one default variant per model;
published implies ready and validated; callers cannot supply `slug` or a local path.

## AcquisitionWorkflow

- `id: UUID` (also the deterministic DBOS workflow ID)
- `model_id`, `variant_id`
- immutable `repo_id`, `revision`, conversion request, `keep_raw`
- `origin_node_id`, `replica_node_id`
- `stage: inspect|pull|convert|validate|replicate|done|failed`
- `state: queued|running|waiting_for_capacity|succeeded|failed|cancelled`
- progress bytes/files, failure code/detail, attempt, timestamps

Only forward stage transitions are valid. Retry moves `failed` to the earliest incomplete stage;
completed stage result rows are immutable and reused.

## AcquisitionStageResult

- workflow/stage composite identity
- status, attempt, started/finished timestamps
- typed JSON result digest and public summary
- node/job identifiers where physical work ran
- failure code and safe message

There is at most one successful result per workflow/stage. Step output contains registry IDs and
digests, never credentials.

## InspectionResult

- immutable revision, architecture, source format, gating state
- metadata/weight/total bytes
- candidate precision estimates and per-placement fit decisions
- source-repository guidance for GGUF
- supported verdict and rejection reason

Inspection success does not imply transfer. Rejected inspection has `bytes_transferred = 0`.

## NodeReservation

- `id`, workflow/job/variant/node IDs
- `kind: conversion`
- memory and disk bytes
- `state: requested|held|released|expired`
- occupants snapshot and timestamps

Held reservations contribute to the same node committed-memory ledger as engines. Release is
idempotent and occurs on success, failure, cancellation, or recovery cleanup.

## ValidationResult

- variant/workflow IDs and validator version
- `smoke: pass|fail`, prompt-set digest, safe failure summary
- `perplexity`, reference variant/perplexity, tolerance, comparison outcome
- `template: pass|fail|not_applicable`, rendered-shape digest
- aggregate `validated`, timestamp

Template failure always makes aggregate validation false. Missing reference produces
`not_comparable`, not pass or fail.

## VariantCopy

Variant-level equivalent of `ModelCopy`: node, path, bytes, manifest digest, verification state,
role, timestamps. A variant becomes ready only with two verified copies sharing a manifest digest.

## State transitions

```text
requested → inspecting ──rejected/failed
                  │
                  ▼
                queued ⇄ waiting_for_capacity
                  ▼
                pulling → converting → validating → replicating → ready
                             │            │             │
                             └────────────┴─────────────┴──→ failed
```

An already-MLX source skips physical conversion but records a successful `convert` stage with
`operation=noop`. A later variant begins at `convert` only when an eligible raw source exists.

## Backward compatibility

Existing `models`, `model_copies`, and `download_jobs` remain during the compatibility release.
Migration seeds a default variant and variant copies from each existing model/copy. Model-level
state and default variant state are projected together until feature 005 removes the legacy shape.
