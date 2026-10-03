# Implementation Plan: Authenticated node image worker routes and shared budget

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Routes are coire-node control only; core loads no weights. |
| II-a | No new container/service. |
| III | Existing coire-core load/unload/result models define every route. |
| IV/V | Node bearer on all routes; exact instance binding and verified model copy. |
| VI | Supervisor launch/load/cleanup telemetry is already instrumented; route errors remain content-free. |
| VII | Route and shared-budget regression tests before code; Studio acceptance remains open. |

## Approach

Add a dedicated `/node/images/worker` router backed by `ImageProcessSupervisor`, mounted only on control/mesh/fallback listeners under the existing bearer. Wire one reentrant memory lock and cross-reservation callbacks into EngineManager and the image supervisor. Adopt image state before binding listeners and use their combined commitment in ReservationLedger. Record the dedicated route choice in an ADR because the parent contract originally proposed extending generic engine routes, while the image worker has distinct fenced commands and lifecycle.
