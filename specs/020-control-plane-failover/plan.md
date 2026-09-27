# Implementation Plan: Control-Plane Failover and Frontend Election

**Branch**: `feat/020-control-plane-failover` | **Date**: 2026-09-05 | **Spec**:
[`spec.md`](spec.md)

## Summary

Provide quorum-gated, stateless inference access when core is unavailable. Core remains the sole
system of record. `coire-api` participates as the primary election member; `coire-node` hosts the
bounded Studio member and a separate hardened `coire-failover` container serves only authenticated
resident-model inference. A signed, read-only snapshot supplies the roster and browser identity
verification material. Cloudflare Load Balancing selects only the elected member's readiness route.

## Technical Context

**Language/Version**: Python 3.13; TypeScript strict for the minimal degraded UI

**Primary Dependencies**: FastAPI, Pydantic v2, httpx, existing Cloudflare Access validation,
Cloudflare Tunnel and Load Balancer

**Storage**: Postgres remains core-only; signed atomic snapshot and bounded append-only election
journal on each Studio are non-authoritative and are reconciled to audit on core recovery

**Testing**: pytest unit/contract/integration, Vitest, compose topology/image-policy gates, and
manual real-cluster power, partition, ingress, and recovery validation

**Target Platform**: macOS 26.2+ on core and Studios; OrbStack containers; Cloudflare-authenticated
public edge

**Project Type**: distributed web service with a React static frontend and typed REST/SSE APIs

**Performance Goals**: no gateway overhead regression in full mode; failover RTO is the existing
health-unreachable threshold plus election, frontend start, and LB monitor propagation; no model
eviction at promotion

**Constraints**: a Studio never runs Postgres, scheduler, MCP, admin/mutation routes, engines, or
telemetry backends; automatic promotion requires a current 2-of-3 lease; lone-survivor service is
break-glass-only; no degraded writes or deferred write buffers; all Studio ingress stays
outbound-only and Access-protected

**Scale/Scope**: fixed three-member cluster: core primary, edge-a secondary, edge-b tertiary;
one elected frontend; resident-model chat/completions and model listing only

## Constitution Check

| Principle | Status | Design response |
|---|---|---|
| I. Bare engines | PASS | Failover proxies only existing node-owned bare engines; it cannot load or manage one. |
| II. Control node / workers | PASS | Constitution v1.0.0 authorises one stateless, inference-only Studio frontend; core remains authoritative. |
| II-a. Bare container | PASS | `coire-failover` is a dedicated hardened image/container with a healthcheck and least-privilege networks. |
| III. Contracts first | PASS | Election, snapshot, peer-relay, status, and degraded responses are strict `coire-core` models. |
| IV. Zero implicit trust | PASS | Signed peer messages, current leases, Access-only degraded browser auth, and scoped peer relay credentials fail closed. |
| V. Models are data | PASS | Snapshot IDs plus live node health permit only published, currently resident models. |
| VI. Observable | PASS | Election, readiness, snapshot, ingress, and hand-back telemetry have dashboard and alerts. |
| VII. Spec-driven, test-gated | PASS | Contract and composed tests plus mandatory real-cluster fault verification ship with the feature. |

No exception beyond Constitution v1.0.0 is required.

## Project Structure

```text
packages/coire-core/src/coire_core/models/failover.py
apps/coire-api/src/coire_api/failover/{membership,snapshot,participant,reconcile}.py
apps/coire-node/src/coire_node/failover/{participant,relay,journal}.py
apps/coire-failover/src/coire_failover/{app,auth,snapshot,election,proxy}.py
apps/coire-web/src/pages/Failover.tsx
deploy/{compose,cloudflared,observability}/
docs/runbooks/control-plane-failover.md
specs/020-control-plane-failover/{research,data-model,contracts,quickstart}.md
tests/integration/test_control_plane_failover.py
```

**Structure Decision**: add one narrowly scoped failover distribution instead of reusing the normal
API, whose database lifecycle, leases, and usage accounting violate degraded-mode constraints.

## Phase 0: Research

See [`research.md`](research.md). The selected design uses signed quorum leases, a verified snapshot,
separate Cloudflare tunnel endpoints with LB readiness probes, and Access-only browser identity in
degraded mode.

## Phase 1: Design

See [`data-model.md`](data-model.md), [`contracts/`](contracts/), and
[`quickstart.md`](quickstart.md).

## Post-Design Constitution Re-check

Passed. The election journal is bounded safety metadata rather than control-plane state; it is
never authoritative, never accepts user writes, and is reconciled idempotently after core returns.
Ingress, readiness, and leases fence an unelected Studio before it can serve.

## Complexity Tracking

| Added element | Why needed | Simpler alternative rejected because |
|---|---|---|
| Separate failover service | Prevent any accidental database lifecycle or write path | Normal API/gateway creates leases and usage rows. |
| Quorum lease protocol | Prevent split-brain during partitions | Host reachability alone cannot distinguish partition from failure. |
| Signed snapshot | Allow safe stateless auth/roster use | A replicated database becomes a Studio source of truth. |
