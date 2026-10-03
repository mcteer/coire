# Implementation Plan: Fixed Studio txt2img pipeline

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Direct pinned mflux in coire-node only; no core weights or wrapper service. |
| II-a | No new service/container. |
| III | Existing `ResolvedImageSpec` is the only request shape; no new wire contract. |
| IV/V | Exact preflight path and offline environment; no caller model string or Hub pull. |
| VI | Existing node stage telemetry is used at the later supervised worker boundary; progress carries no prompt. |
| VII | Fake-engine behavior tests precede code; live acceptance remains open. |

## Approach

Add an in-process pipeline module with a lazy native loader, explicit offline check, strict Turbo policy, synchronized callback and PIL result validation. The node worker will later own this pipeline's process lifetime, cancellation, output persistence and telemetry.
