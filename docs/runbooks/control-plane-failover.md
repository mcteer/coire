# Control-plane failover

Core stays the system of record. A Studio serves inference only after it holds a current quorum
lease or an unexpired break-glass override. Postgres, the scheduler, MCP, admin routes, and
telemetry backends never move.

## See it

- Grafana dashboard `coire-failover`: readiness, snapshot freshness, elections, hand-backs, refusals.
- Alerts: `CoireFailoverNoReadyMember`, `CoireFailoverSnapshotStale`, `CoireFailoverSplitBrain`,
  `CoireFailoverIngressRefusals`.
- Core's public monitor is `GET /failover/ready` (200 only with a lease). Studio monitors are
  `GET /ready` on `coire-failover`. Anything else fails closed. Process liveness stays on core's
  `/ready` and is not the load-balancer probe.
- Audit actions `failover.promoted`, `failover.draining`, `failover.fenced`, `failover.handback`,
  `failover.break_glass`, and `failover.override.*` after reconciliation.

## Activate

1. Confirm three healthy members and a fresh signed snapshot on each Studio
   (`FAILOVER_SNAPSHOT_PATH`, mode `0644`; it contains signed public metadata).
2. Confirm distinct Keychain items `coire-failover-peer-key` and `coire-failover-relay-token`.
   Do not reuse a node registration token.
   Confirm `FAILOVER_CORE_PUBLIC_KEY` matches the separately provisioned core signing key,
   and both Studio relay URLs name their control listeners from inside the frontend container.
3. On each Studio, set `COIRE_FAILOVER_IMAGE` to the same digest-pinned image installed as
   `FAILOVER_FRONTEND_IMAGE` in coire-node. Set `COIRE_FAILOVER_LOCAL_RELAY_URL` and
   `COIRE_FAILOVER_PEER_RELAY_URL` to the two Studio control-listener URLs. Run
   `deploy/compose/coire-studio-failover-create`. It materializes the Keychain relay
   credential outside the repository and creates the container with `--no-deps`.
   The node starts this precreated container only after election and stops it after
   hand-back or fencing.
4. Confirm only core's `/failover/ready` is 200 while core is healthy.

Thresholds default to promotion 15s, demotion 45s, lease 15s, drain 30s, and a poll every 2s.
A heartbeat slower than 50ms is degraded. Three missed beats mark a peer unreachable, and the
promotion threshold is then counted from the first miss. Demotion must stay longer than
promotion. Changing them is a reviewed recovery edit, not a hot fix. Each host sets
`FAILOVER_MEMBER_NAME` to its own name; an empty name leaves the poller off.

## Break glass

A lone Studio does not promote. An admin posts a core-signed override:

`POST /api/v1/admin/failover/overrides`

Body: `kind` `break_glass_promote` or `inhibit`, `actor_id`, `reason`, `expires_at`, `signature`.
The signature is Ed25519 over the canonical JSON without the signature field, using the core
peer key. The row is audited. The override stops working at `expires_at` with no further action.
`inhibit` keeps Studios from promoting while core is still the authority.

## Partition

- A member that sees fewer than two hosts fences. Its lease file is removed and `/ready` is 503.
- Core in a minority partition returns 503 from `/failover/ready` and the load balancer stops
  using it. `/ready` on the API process stays a liveness check so compose does not restart it.
- Two ready members is a split brain. Treat `CoireFailoverSplitBrain` as a page and fence the
  member whose proof is older.

## Hand-back

When core has been healthy for the demotion threshold, the elected Studio drains in-flight
completions, deletes its lease, and stops the emergency frontend. The standing 256 MiB ledger
reservation remains held for the next failover. Core waits out the same
window before it campaigns, so a flap does not oscillate. New requests stop as soon as drain
starts; requests already running are allowed to finish.

## Roll back

1. `docker compose --profile studio-failover stop coire-failover` on both Studios.
2. Remove `/opt/coire/failover/proof.json` if a lease file remains.
3. Point the load balancer at core only, or leave the empty fallback so a dark Studio is not used.
4. Do not delete `failover_event_receipts`; reconciled audit rows stay.

Record timings and the elected host in `specs/020-control-plane-failover/review.md`. Do not record
keys, tokens, or model bytes.
