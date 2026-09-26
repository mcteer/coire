# Quickstart: Control-Plane Failover

## Preconditions

- All three members are healthy; each has a current verified snapshot.
- Separate core, edge-a, and edge-b tunnel endpoints and LB readiness monitors are provisioned.
- A resident published test model exists on each required Studio.

## Validate

1. Confirm only core readiness is successful and the public hostname is Access-protected.
2. Power off or isolate core. After the configured health, election, and LB-monitor windows, confirm
   only edge-a readiness succeeds and an authenticated browser request completes against the resident
   model. Confirm no database, admin, MCP, scheduler, or write route exists on edge-a.
3. Partition edge-a from core and edge-b. Confirm it self-fences and never serves without quorum.
4. Make core and edge-a unavailable. Confirm edge-b does not auto-promote; exercise the expiring
   break-glass path and confirm its event reconciles after recovery.
5. Restore core. Confirm edge-a drains in-flight requests, fences readiness, core regains the lease,
   and the LB returns traffic to core without flapping.
6. Tamper with or age a test snapshot. Confirm degraded inference refuses authentication and no
   unauthenticated request succeeds.

Record timings, elected host, ingress monitor states, traces, and reconciled audit events. Do not
record credentials, private keys, or model contents.
