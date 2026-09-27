# Model instances and cluster state

Inspect `GET /api/v1/state`, then one lifecycle at `GET /api/v1/instances/{id}` and its persisted
SSE stream at `/events`. Grafana links instance metrics to `coire.scheduler.instance.*` traces.

Stop safely with `DELETE /api/v1/instances/{id}`. It enters `draining`, rejects new work, waits for
leases, and stops by `INSTANCE_DRAIN_TIMEOUT_S`. Do not stop engines directly except for failure tests.

For a stalled launch, inspect the instance, placement decision, `placement_commands`, scheduler logs
with `instance_id`, and node health. Restarting only the scheduler is safe because DBOS reattaches.

Node declaration returns its registration token once. Put it directly in the Studio System Keychain;
never store it in files, history, audits, or issues. Rotation and revocation are audited.

Rollback drains instances, rolls API/scheduler images together, then downgrades `0007` only after
confirming no multi-instance reservations would collapse onto one legacy model holder.
