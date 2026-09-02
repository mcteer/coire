# Placement scheduler and auto-unload

Spec: `specs/004-placement-scheduler/spec.md`

Adds the reservation-authoritative per-node memory ledger, serialized DBOS placement decisions,
LRU eviction, pin immunity, gateway request leases, idle-TTL unload, authenticated API-side node
command execution, operator routes, observability, alerts, and a rollback runbook.

## Constitution compliance

- **I — Bare engines:** load/unload still targets the node-owned bare `mlx_lm.server` lifecycle;
  no inference wrapper or engine port is exposed.
- **II — Core hosts no models:** core persists and schedules decisions only; all engine processes
  remain on Studios.
- **II-a — One service, one container:** no service was combined and all changed production images
  pass the distroless/non-root/read-only/capability policy.
- **III — Contracts first:** every ledger/placement wire shape is strict Pydantic; OpenAPI and the
  generated TypeScript client schema are updated.
- **IV — Zero implicit trust:** admin routes are guarded and audited; only the API executor holds
  node credentials.
- **V — Models are data:** placement accepts registry/variant UUIDs and only validated variants;
  callers cannot pass an engine model path.
- **VI — Observable:** placement spans, bounded metrics, dashboard panels, drift/capacity/queue
  alerts, structured correlation IDs, and an operator runbook ship with the workflow.
- **VII — Spec-driven/test-gated:** unit, contract, composed restart/pressure, web, OpenAPI, image
  policy, and CRITICAL CVE gates pass; live tiny-model evidence is explicitly left open.
