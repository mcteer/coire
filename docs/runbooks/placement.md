# Placement and memory-ledger operations

The PostgreSQL reservation ledger is the admission authority. Node resident-memory samples are
diagnostic drift inputs only; do not change admission to use instantaneous free memory.

## Inspect

Use an admin token and request `GET /api/v1/admin/ledger`. Each node reports its budget,
reserved and free bytes, health freshness, measured residency, drift ratio, and every holder.
Inspect an individual durable decision at `GET /api/v1/admin/placements/{decision_id}`. Correlate
that UUID with the `coire.scheduler.placement` trace and structured scheduler/API logs.

## Change a budget or sandbox slice

Patch `/api/v1/admin/ledger/{node_id}` with `budget_bytes` and/or `sandbox_bytes`. A reduction
below current reservations is safe: it prevents further admission and does not kill work. Every
change is audited. Restore the former values to roll back.

## Pin, unpin, and evict

Patch `/api/v1/admin/ledger/reservations/{reservation_id}` with `{"pinned":true}` or `false`.
Pinned reservations are excluded from pressure and TTL eviction. To perform an explicit engine
kill, use `DELETE /api/v1/admin/engines/{engine_id}`; never call a bare engine or node endpoint
from an operator workstation.

## Idle TTL and stuck work

The DBOS idle loop evaluates each model's `idle_ttl_seconds`; an active request lease or pin
blocks unload. `coire_placement_queue_seconds`, refusal counts, and ledger drift are visible in
the Acquisition Jobs dashboard. For a stuck decision, inspect its persisted placement command
and node health first. Restarting `coire-api` or `coire-scheduler` is safe: pending commands and
DBOS workflows resume by deterministic ID.

## Rollback

Stop new placement requests, let running requests drain, and restore the preceding API and
scheduler images together. Migration `0006` is reversible only after placement rows are no
longer needed; back up PostgreSQL before downgrading. Existing node engines continue serving
while the control plane rolls back.
