# Model instances and cluster state

Inspect `GET /api/v1/state`, then one lifecycle at `GET /api/v1/instances/{id}` and its persisted
SSE stream at `/events`. Grafana links instance metrics to `coire.scheduler.instance.*` traces.

Stop safely with `DELETE /api/v1/instances/{id}`. It enters `draining`, rejects new work, waits for
leases, and stops by `INSTANCE_DRAIN_TIMEOUT_S`. Do not stop engines directly except for failure tests.
The gateway refuses a node engine request with `chat_model_unavailable` if its
shared memory reservation is missing, rather than proxying an inference with
no request lease. Inspect the engine ID, instance member, reservation state
and placement command before restoring traffic; recover through placement.

For a stalled launch, inspect the instance, placement decision, `placement_commands`, scheduler logs
with `instance_id`, and node health. Restarting only the scheduler is safe because DBOS reattaches.
For a visual launch refused as an incomplete or mismatched local copy, inspect the node's local
processor inventory and manifest verification result. Retry only through the audited admin
acquisition path; do not repair weights or processor files by hand. The node checks the visual
manifest again before process spawn and strips Hub credentials from the engine environment.

The admin API returns a node registration token once. Store it as `coire-node-registration-token`
in the Studio System Keychain, separately from `coire-node-token`, which authenticates core-to-node
commands. From core, copy the issued token to its clipboard and run `COIRE_REG_TOKEN="$(pbpaste)"`.
Then run `ssh -tt mcteer@coire-edge-a.lab "sudo security add-generic-password -a coire -s
coire-node-registration-token -U -w '$COIRE_REG_TOKEN' /Library/Keychains/System.keychain"`
(substitute the declared Studio host). Immediately run `unset COIRE_REG_TOKEN` and clear the
clipboard. The command contains no literal token in shell history and prompts for Studio sudo.
macOS rejects `-w` with no value after the keychain path.
Restart `com.coire.node` after adding it. A successful registration writes only its SHA-256
fingerprint to `/opt/coire/state/registration-success.sha256`; subsequent agent restarts skip
registration. If registration is refused, inspect the API `coire_node_registration_attempts_total`
counter and registration audit, then check the declared node endpoints and issue a fresh token via
the admin API. Never store the plaintext token in files, history, audits, or issues. Rotation and
revocation are audited. A replacement token changes the fingerprint and registers once on restart.

Rollback drains instances, rolls API/scheduler images together, then downgrades `0007` only after
confirming no multi-instance reservations would collapse onto one legacy model holder.

## Locked native node environment

On the operator build Mac, run `scripts/build-node-wheel.sh --local-only` to stage the exact
`uv.lock` native node graph in `dist/node-wheels` without contacting a Studio. The normal
`scripts/build-node-wheel.sh <node-name>` command copies that wheelhouse and the installer to
the named Studio over the control network. Run the staged `install.sh --wheel-dir
~/coire-stage/dist` there after creating the `/opt/coire` prefix as described by `install.sh
--help`. The installer provisions pinned CPython, installs the hash-checked dependency wheels
without an index, then installs the local core and node wheels. It imports both engines and runs
their `--help` commands before changing `/opt/coire/envs/current`; this smoke does not load a
model or start a server. The environment name includes a digest of the locked requirements and
local wheels, so an existing active environment is never modified.

If install or smoke fails, inspect the installer output and the staged wheelhouse. The prior
`envs/current` link remains active. To roll back a later activated environment, drain instances,
stop `com.coire.node`, and atomically replace `envs/current` with a symlink to the prior known-good
versioned directory. For example, run `python3 -c 'import os; os.symlink("/opt/coire/envs/<prior>", "/opt/coire/envs/.rollback"); os.replace("/opt/coire/envs/.rollback", "/opt/coire/envs/current")'`
after substituting the actual prior directory. Restart the service and inspect node health/engine
ownership.
Keep the prior directory until that check passes. The installer does not prove tiny-model
generation, visual token usage, cancellation or cluster placement; run the feature 014 acceptance
checks before enabling visual Chat.

After an agent restart, a surviving engine keeps its memory reservation and reports
`starting` while the node proves a one-token generation again. A live PID alone does
not establish readiness. Inspect `GET /node/engines`, the
`coire_engine_adoption_total` outcomes, the `coire.node.engine_adopt` span and
the `CoireEngineAdoptionRecheckFailed` alert. If an adopted engine remains
unresponsive, drain its instance through the admin API, stop the exact engine
through the authenticated node control API, and reload it through normal
placement. Confirm the new engine reaches `ready` and serves a short request
before restoring traffic. The old process must be dead before its reservation
is released; keep the previous immutable environment available for rollback.
