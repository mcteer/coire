# Research: Control-Plane Failover and Frontend Election

## Election

**Decision**: Use a fixed, core-signed three-member configuration and quorum leases. A candidate
requires two current signed grants for one `{epoch, term, candidate}`; a voter grants one lease per
term. Core also self-fences when it lacks its own quorum lease.

**Rationale**: A lone edge-b cannot tell a real two-host failure from a partition. Majority quorum
intersection prevents two correct participants from holding a valid lease. A lone survivor therefore
fails closed unless an audited, expiring break-glass promotion is supplied.

**Alternatives considered**: Reachability-only priority election was rejected because it permits
split-brain. Replicating Postgres was rejected because it creates a Studio source of truth.

## Snapshot and degraded authority

**Decision**: Core atomically publishes a signed, expiring, read-only snapshot. Degraded browser
requests use Cloudflare Access identity only; API keys are unavailable during failover. The service
authorises a model from the snapshot and confirms it is currently resident from live node health.

**Rationale**: Copying API-key hashes makes revocation stale and expands secret material. Snapshot
plus live health supplies only the minimal authority needed for safe browser inference, without a
database, request lease, usage write, or cold load.

**Alternatives considered**: Running normal `coire-api` was rejected because its lifecycle and
gateway paths require Postgres and create mutable records. A local replicated database or deferred
write buffer was rejected as a source-of-truth expansion.

## Service boundary

**Decision**: Add a separate `coire-failover` service exposing only readiness/tier, `/v1/models`,
and `/v1/chat/completions` (plus `/v1/messages` only if the public compatibility surface requires
it). All other routes are absent.

**Rationale**: Absence is enforceable by route sweeps and image inspection. Reusing the normal app
would make forbidden dependencies and stateful behavior too easy to introduce.

## Ingress

**Decision**: Use separate Cloudflare Tunnel UUIDs for core, edge-a, and edge-b, with a Cloudflare
Public Load Balancer on the stable Access-protected hostname. Each pool monitor probes a no-cache
election-gated readiness route; unelected, stale-snapshot, draining, and standby endpoints return
non-success. The fallback is fail-closed.

**Rationale**: Tunnel replicas do not provide deterministic traffic steering. The LB preserves
outbound-only Studio connectivity and routes only to a proven serving tier. RTO must include the
health threshold, election, container start, and LB monitor propagation.

**Sources**: [Cloudflare Tunnel routing](https://developers.cloudflare.com/tunnel/routing/),
[standard load-balancer steering](https://developers.cloudflare.com/load-balancing/understand-basics/traffic-steering/steering-policies/standard-options/),
[health details](https://developers.cloudflare.com/load-balancing/understand-basics/health-details/),
and [Raft](https://raft.github.io/raft.pdf).

## Peer communication and recovery

**Decision**: Core participates in `coire-api`; Studios participate in `coire-node`. Signed peer
heartbeats use existing control/fabric listeners with a narrow election route. Cross-Studio inference
relay receives a distinct Keychain-sourced, least-privilege credential. A bounded local election
journal is reconciled idempotently to core audit after hand-back.

**Rationale**: Existing node registration tokens are broad and per-node; they must not become peer
or inference-relay credentials. On hand-back, the Studio first stops new requests, drains to a
deadline, fences readiness, and stops the frontend. The standing ledger reservation remains held for the next failover.
