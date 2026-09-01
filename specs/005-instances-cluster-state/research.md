# Research: Model Instances and Cluster State

## R1 — Persist transitions

Use `instance_transitions` with monotonic per-instance sequence numbers. SSE replays committed rows
after `Last-Event-ID`, then polls. This survives API restarts and serves late terminal subscribers.

## R2 — DBOS identity supplies lifecycle idempotence

Workflow id is `instance-<uuid>`. Steps lock the instance row and use commands keyed by instance id.
A restart therefore reattaches rather than creating a second reservation or engine.

## R3 — Instance is the reservation holder

Feature 004 used model UUID as `holder_id`. Rekey held model reservations to instance ownership and
record `instance_id` on engines, allowing multiple placements of one model.

## R4 — Gateway selection

Select only ready instances on healthy nodes. Prefer affinity, then least in-flight and stable UUID.
Acquire the instance reservation's request lease before proxying. Draining is immediately ineligible.

## R5 — Drain semantics

Reject new admissions, wait for active leases, then stop. At `INSTANCE_DRAIN_TIMEOUT_S`, expire the
remaining leases, stop anyway, and record a forced drain.

## R6 — Declared-node tokens

Admin declaration returns 32 random bytes once. Persist an HMAC-SHA256 digest using the signing secret
plus issued/consumed/revoked timestamps. Compare in constant time and audit every outcome. There is no
broadcast, multicast, subnet scan, or node-row creation during registration.

## R7 — Compatibility

Migration creates a legacy instance per existing engine and rekeys held reservations. Existing
placement endpoints adapt to instance creation. OpenAI shapes stay compatible; instance identity is
additive as `coire_instance_id`.
