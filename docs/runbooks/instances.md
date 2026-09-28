# Model instances and cluster state

Inspect `GET /api/v1/state`, then one lifecycle at `GET /api/v1/instances/{id}` and its persisted
SSE stream at `/events`. Grafana links instance metrics to `coire.scheduler.instance.*` traces.

Stop safely with `DELETE /api/v1/instances/{id}`. It enters `draining`, rejects new work, waits for
leases, and stops by `INSTANCE_DRAIN_TIMEOUT_S`. Do not stop engines directly except for failure tests.

For a stalled launch, inspect the instance, placement decision, `placement_commands`, scheduler logs
with `instance_id`, and node health. Restarting only the scheduler is safe because DBOS reattaches.

The admin API returns a node registration token once. Store it as `coire-node-registration-token`
in the Studio System Keychain, separately from `coire-node-token`, which authenticates core-to-node
commands. Use `sudo security add-generic-password -a coire -s coire-node-registration-token -U
/Library/Keychains/System.keychain -w` on the Studio and paste the issued token at its hidden prompt.
Restart `com.coire.node` after adding it. A successful registration writes only its SHA-256
fingerprint to `/opt/coire/state/registration-success.sha256`; subsequent agent restarts skip
registration. If registration is refused, inspect the API `coire_node_registration_attempts_total`
counter and registration audit, then check the declared node endpoints and issue a fresh token via
the admin API. Never store the plaintext token in files, history, audits, or issues. Rotation and
revocation are audited. A replacement token changes the fingerprint and registers once on restart.

Rollback drains instances, rolls API/scheduler images together, then downgrades `0007` only after
confirming no multi-instance reservations would collapse onto one legacy model holder.
