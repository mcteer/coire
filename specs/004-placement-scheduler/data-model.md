# Data Model: Placement Scheduler and Auto-Unload

## NodeMemoryLedger

- `node_id` UUID primary/foreign key
- `budget_bytes` bigint, positive
- `sandbox_bytes` bigint, non-negative, default 16 GiB
- `measured_resident_bytes` bigint nullable
- `health_state`, `health_reason`, `health_sampled_at`
- `updated_at`

Derived: `reserved_bytes`, `free_bytes`, and drift ratio. Budget reductions are allowed even when
free becomes negative; new admissions are blocked.

## MemoryReservation

- `id` UUID primary key; `node_id` foreign key
- `holder_type`: `sandbox | model | conversion | training | image | run`
- `holder_id` UUID/string stable identity
- `bytes` bigint positive
- `pinned` boolean
- `state`: `pending | held | releasing | released | failed`
- `last_used_at`, `created_at`, `released_at`
- unique active identity `(node_id, holder_type, holder_id)`

Only `held`/`releasing` bytes count. Release occurs after confirmed unload.

## RequestLease

- `id`, `reservation_id`, `request_id`, `expires_at`, `created_at`, `released_at`
- active non-expired leases make a model ineligible for eviction/TTL.

## PlacementDecision

- `id`, idempotency key, model/variant, requested policy, selected node
- `state`: `requested | waiting_for_drain | evicting | reserving | loading | ready | refused | failed`
- required bytes, evicted reservation IDs, refusal code/detail, timestamps

## EvictionEvent

- decision, node, reservation/model, LRU rank, trigger, skipped candidates/reasons, outcome, timestamp

All admin pin/budget changes and all eviction/refusal/TTL decisions also append audit rows.
