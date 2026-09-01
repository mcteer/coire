# Data Model: Container Run Orchestration

## AgentRun

- `id`: UUID; also the DBOS workflow id and idempotency key
- `requester_user_id`: authenticated user
- `profile`: coding, general, or image
- `primary_model_id`: registry model UUID
- `node_id`: nullable until placed; must reference a Studio node
- `container_id`: nullable Docker identity; unique when present
- `workspace_ref`: platform-created opaque workspace reference
- `state`: queued, placing, creating, running, collecting, succeeded, failed,
  result_collection_failed, timed_out, kill_requested, killed
- `limits`: memory bytes, nano CPUs, PID limit, wall-clock seconds, log/result byte ceilings
- `exit_code`, `failure_code`, `failure_detail`, `result`, `resource_usage`
- `requested_at`, `started_at`, `finished_at`, `updated_at`
- `killed_by`, `killed_at`

Terminal states are immutable. A kill request revokes the token before node contact. State changes
append an `AgentRunTransition` row.

## RunToken

- `id`: UUID
- `run_id`: unique AgentRun reference
- `secret_hash`: Argon2id hash; bearer plaintext is never persisted
- `permitted_model_ids`: non-empty registry UUID set
- `permitted_tools`: bounded profile-derived strings
- `spend_limit_tokens`, `spent_tokens`
- `expires_at`, `revoked_at`, `created_at`

A token is valid only when its hash matches, its run is active, server time is before expiry,
`revoked_at` is null, requested model/tool is in scope, and spend remains.

## RunCommand

- `id`: UUID
- `run_id`: AgentRun reference
- `operation`: create, start, logs, wait, collect, remove, kill, reconcile
- `attempt`, `state`, `node_id`, `detail`, timestamps

Commands provide audit-grade durable intent around external effects; `(run_id, operation, attempt)`
is unique.

## RunSlot

The current count is derived from non-terminal AgentRuns per node. The configurable cap lives on
the node record/settings and defaults to three. A zero sandbox slice makes the node ineligible.

## RunContainerObservation

- `run_id`, `node_id`, `container_id`, observed state, labels, exit code, resource usage, observed_at

Observations support recovery and orphan reconciliation without treating Docker as the source of
authorization.

## State transitions

```text
queued -> placing -> creating -> running -> collecting -> succeeded
   |         |          |          |            |-------> result_collection_failed
   |         |          |          |--------------------> failed/timed_out
   |         |          |-------------------------------> failed
   |         |------------------------------------------> failed
   |----------------------------------------------------> kill_requested -> killed
```

Any active state may enter `kill_requested`; terminal transitions revoke the token and release the
slot. Recovery observes an existing labeled container before issuing another external effect.
