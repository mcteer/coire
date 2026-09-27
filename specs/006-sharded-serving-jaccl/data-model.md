# Data Model: Sharded Serving over JACCL

## `studio_links`

Canonical node pair; projected IP/RDMA/fallback state; bandwidth and latency; probe kind/version;
measured/required-after timestamps; consecutive successes/failures; flapping window/count; reason.
Unique on `(node_a_id,node_b_id)` with canonical ordering.

## `link_observations`

Append-only probe evidence: link, transport (`jaccl|ring`), outcome, bandwidth, latency, OS and
engine versions, observed time and redacted reason. No hostfile contents or credentials.

## `shard_groups`

One-to-one with a sharded model instance: mode (`tp|pp`), coordinator node, group state,
architecture, hostfile digest, launch/stop command IDs and timestamps. The hostfile itself remains
node-local generated state.

## `instance_members`

Existing rows represent ranks 0 and 1. Each has node, reservation, engine/group command identity,
host, port, pid/create-time projection and health. Unique `(instance_id,rank)` and
`(instance_id,node_id)`.

## `benchmark_results`

Append-only model variant, placement, tokens/s, prompt/generated token counts, warm/cold flag,
node GPU-core snapshot, engine/OS versions, run time and optional failure. Repeated runs insert.

## Invariants

- A sharded group has exactly ranks 0 and 1 on distinct declared Studios; core cannot be a member.
- Both held reservations share the instance holder and are committed/released as one transaction.
- TP is eligible only from a current successful RDMA observation; latency never decides eligibility.
- A failed rank implies failed instance and no surviving ready group.
- Benchmark and link observations are append-only; projections may change.

