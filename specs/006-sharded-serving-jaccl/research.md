# Research: Sharded Serving over JACCL

## R1 — Process ownership

**Decision:** rank 0's coire-node owns an explicit `mlx.launch` coordinator process; rank 1 accepts
only a group-scoped authenticated prepare/stop command. Both persist group/rank identity before
spawn. **Reason:** this follows Principle I and permits adoption/teardown without SSH from core.

## R2 — Hostfile source

**Decision:** generate the two-rank JSON from declared `data_host` values ordered A then B and
reject core or missing/duplicate data endpoints. Write atomically beneath node state. **Reason:**
inventory is authoritative and the direct Thunderbolt fabric is Studio-only.

## R3 — Atomic admission

**Decision:** take PostgreSQL advisory locks for both node UUIDs in sorted order inside one
transaction, calculate eviction on both, then insert both reservations or neither. **Reason:**
consistent ordering prevents split reservation/deadlock and the transaction provides all-or-none.

## R4 — Link eligibility and damping

**Decision:** persist raw observations and a projected verdict. Two consecutive failures mark down;
three consecutive successes restore up. TP requires a current successful RDMA probe. Latency and
bandwidth are measurements/alerts only. PP may use the measured ring/TCP fallback. **Reason:** this
fails closed without reintroducing the explicitly rejected sub-50 ms policy.

## R5 — Rank failure

**Decision:** any missing/unhealthy rank atomically fails the instance, marks that node degraded,
issues idempotent stops for all ranks, and releases both reservations only after confirmation.
Gateway streams translate group loss to an SSE error plus HTTP `Retry-After` metadata where the
protocol permits. **Reason:** partial groups are never useful or safe.

## R6 — Fallback placement

**Decision:** make one durable, idempotent search for the largest verified default-compatible
variant that fits the healthy survivor; create a `single:<node>` instance or persist `no_fit` and
alert. **Reason:** bounded recovery avoids thrash and never invents a caller model path.

## R7 — Test boundary

**Decision:** Linux composed tests use a fake group command with two independently killable rank
processes and deterministic link observations. Real JACCL remains a required manual gate.
**Reason:** CI cannot truthfully emulate Apple RDMA, but can fully test orchestration invariants.

## R8 — Launcher rank count and generated inventory

**Decision:** invoke `mlx.launch --backend <jaccl|ring> --hostfile <generated> --env
MLX_METAL_FAST_SYNCH=1 -- <python> -m mlx_lm.server ...` without `-n 2`. A two-host inventory
already starts one process on each host; MLX defines `-n` as processes repeated per host, so it
would incorrectly create four ranks. Preserve the complete `mlx.distributed_config` output rather
than synthesizing RDMA device fields. **Source:** upstream MLX distributed launch documentation,
checked 2026-09-01.
