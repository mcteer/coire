# Data Model: Control-Plane Failover

| Entity | Required fields | Rules |
|---|---|---|
| MembershipConfig | epoch, members, priority, peer public keys, snapshot key id | Core-signed; immutable during a failover term. |
| VoteRequest / Grant | epoch, term, candidate, voter, nonce, expiry, signature | One current grant per voter/term; bounded age and signature required. |
| LeadershipLease | proof, holder, expiry, tier | Two distinct grants required; loss fences new requests. |
| FailoverSnapshot | id, issued/expiry, models, Access verifier, peer keys, signature | Canonical JSON, atomically written, signature verified, never mutated on a Studio. |
| FailoverModel | registry UUID/slug, published state, entitlement, limits, template metadata | No paths, copies, placement policy, or acquisition data. |
| TierStatus | tier, elected host, reachable members, snapshot freshness, unavailable capabilities | `full`, `degraded_inference`, or `minimal`; emitted on every response. |
| FailoverEvent | id, term, kind, host, time, proof digest, reconciliation state | Local bounded journal; idempotently reconciled to core audit. |
| Override | kind, actor, reason, expiry, signature | Only `inhibit` or `break_glass_promote`; expires automatically. |

State transitions: `standby → candidate → elected → draining → standby`; an invalid/expired
lease, snapshot, quorum, or readiness probe transitions directly to `standby`. A break-glass leader
is `elected` only until its signed override expires or core hand-back completes.
