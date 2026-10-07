# ADR 0012: Bare SFT training with full checkpoints and exact inference targets

**Date**: 2026-10-03
**Status**: Accepted for implementation; runtime and cluster acceptance remain test gates.
**Feature**: [016 SFT training jobs](../../specs/016-sft-training-jobs/spec.md)

## Context

The pinned mlx-lm 0.31.3 command restores adapter weights but creates a new optimizer. Its
training API exposes optimizer, loss, iterator and callback hooks that permit Coire to retain
full update-boundary state without forking the numerical loop. Current base-model routing,
run credentials and evaluation storage also do not yet identify trained adapters exactly.

## Decision

Only coire-node starts native Studio training workers. They call the unchanged bare MLX/mlx-lm
loader, adapter transformation, `train()` and `evaluate()` APIs with validated local manifests,
remote-code disabled and no Hub credentials. Caller paths, executable model configuration and
remote datasets are refused. Uploaded text/tool JSONL is schema-checked on core; tokenization
and all model work stay on Studios. No wrapper service is added. Direct runtime declarations of
the already-locked PyYAML (MIT) and safetensors (Apache-2.0), and synthetic test NumPy
(BSD-3-Clause), must retain the exact pins and licence rationale in the release summary.

The callback checkpoints only completed optimizer updates, after all accumulation is applied.
Capture trainable parameters, optimizer moments/step/schedule, current MLX RNG key, sampler
RNG/cursor, immutable input/runtime digests and rank identity. Use safetensors and bounded strict
JSON, not pickle. Stage/fsync/rename immutable bundles, mirror the complete rank bundle on both
Studios over scoped data-fabric grants, and advertise durability only after a fenced core commit
verifies both copies. Core holds metadata and uploaded dataset bytes, never tensor artifacts.
An incomplete bundle cannot replace the latest complete checkpoint. Live trainers are re-adopted
by owned attempt/process identity; uncertainty holds reservations rather than causing respawn.

An adapter is an immutable registry record fixing its base variant and manifest. Its advertised
pair selector resolves to an exact typed target; callers never supply engine adapter paths.
Dedicated base-plus-adapter instances, request resolution, run credentials, usage and harness
evaluation carry the exact target. A verified base does not verify an adapter. Publication is an
audited admin action after validation and two-copy readiness. Adapter failover and sharded adapter
serving are not part of 016; adapter engines cannot masquerade as resident base engines.

Training/analysis reuse the same upstream conversation preprocessing and tokenizer template
primitives as bare serving on the Studio. Core shares canonical serialization only. Pin effective
template content, tools and thinking settings. Raw text and final-assistant supervision are explicit;
prompt/padding masks and inference generation suffixes are intentional differences, not drift.

Atomic per-node admission accounts for full data-parallel weights/optimizer state on each Studio,
protects pins and active chat, and prevents image/training overlap. Mixed chat/training requires
exact measured profiles and local watchdog enforcement. Complete-update pause is bounded at
60 seconds with forced-stop fallback; healthy cancellation stops owned groups within 5 seconds.

## Verification and consequences

Same-runtime/hardware/world-size recovery requires exact restored keys/shapes/dtypes, optimizer
steps, RNG and sample sequence. Compare FP32 adapter/moment tensors at `rtol=1e-5, atol=1e-6`
and losses at `rtol=1e-4, atol=1e-5` for the first and next 32 resumed updates. Nonzero dropout,
accumulation and changing schedules plus negative reset controls are mandatory. These tolerances
are acceptance bounds, not a cross-hardware reproducibility promise.

Fixed-workload 15-minute baseline/mixed tests need 100 completed requests per resident target in
each phase and chat first-token p95 <=1.5 seconds for <=4k prompts, positive training progress and
no swap growth. Runtime guard evidence uses a trailing five-minute window with 30 samples per
target and <=60-second freshness. Missing evidence cannot approve new mixed admission.

The feature stays default-off until its gates pass. Contract tests, real Postgres races, offline
tiny-model numerical tests, Studio fault/coexistence trials, alert/dashboard checks and packaging/
rollback evidence remain required. The full implementation plan records initial capability limits.

Baseline observability uses durable progress/loss and audit independently of optional historical
trace/log storage. Event counters are not a stalled-job detector or checkpoint durability proof.
The [training runbook](../runbooks/sft-training.md#baseline-wiring) specifies the pending bounded
snapshot/age/guard gauges, missing-baseline alert, image provisioning dependencies and operational
disable/drain/fence/preserve procedure. A missing series means unknown, never healthy zero.
Actual emission, diagnostics-on attribution and diagnostics-off alert delivery remain acceptance
gates; local synthetic Prometheus tests do not prove those runtime behaviors.

The current enable gate also blocks training history/control routes. Release must preserve
authorized observation/cancellation and reconciliation while disabling new admission; a feature
flag alone is not proof of a safe drain. Migration 0031 deliberately refuses live jobs, retained
lineage/artifacts and populated exact-target projections. Preserve compatible additive schema or
use a validated backup/explicit reviewed migration; never clear references to defeat refusal.

As of 2026-10-04, both original Studio bridges recovered after the network incident. Live direct-NIC
trials remain suspended; the retired/suspended helper is not an operational recommendation. Bridge
recovery does not validate two-rank training. The full real single/two-Studio parameterization,
restart/fault/coexistence matrix, packaging and rollback measurements remain explicitly pending.

## Constitution alignment

- **I**: unchanged bare engine APIs and node-owned lifecycle; no inference/training wrapper.
- **II**: no model/tokenizer/Metal work on core; mirrored artifacts and core metadata remain recoverable.
- **II-a**: existing separate hardened service images; native workers use the node-owned exception.
- **III**: shared strict Pydantic contracts, additive compatibility and generated OpenAPI/TypeScript.
- **IV**: admin audit, scoped short-lived grants, exact run authority, fencing and kill controls.
- **V**: admin-acquired bases, measured capability and independently verified/published adapters.
- **VI**: durable progress/audit plus spans, metrics, bounded logs, dashboard and baseline alerts.
- **VII**: spec-first incremental work and required tiny-model/real-cluster acceptance gates.
