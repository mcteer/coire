# Container runs

User harnesses run only on the Studios. Core persists and schedules the run; `coire-node` is the
only process that talks to the Studio's local Docker socket. Each run has a hardened agent
container and a gateway-only relay on an internal per-run network.

## Observe

```bash
coire --token "$COIRE_API_TOKEN" run list
coire --token "$COIRE_API_TOKEN" run show RUN_ID
coire --token "$COIRE_API_TOKEN" run events RUN_ID
```

Use the `Coire Container Runs` dashboard for transitions, command outcomes, capacity waits, and
orphan reaping. Correlate structured logs by `run_id`, `node`, and `command_id`. On a Studio,
`docker ps --filter label=com.coire.agent-run=RUN_ID` must show at most the agent and relay; neither
publishes a port. Do not inspect container environments because they contain the ephemeral token.

## Kill

```bash
coire --token "$COIRE_API_TOKEN" run kill RUN_ID --reason "operator response"
```

The API revokes the token in its transaction before node contact. A responsive Studio kills and
removes both containers; an unavailable Studio is retried and reconciliation reaps them when it
returns. Confirm state `killed` and an `agent_run.kill` plus terminal audit row.

## Diagnose

- `queued`/`placing`: inspect Studio health, sandbox slice, `RUN_CONCURRENCY_CAP`, and capacity-wait
  metrics. Never enable core fallback.
- `creating`: verify OrbStack is running, the node can read its local Docker socket, both image
  settings are digest-pinned, and the workspace exists directly below `RUN_WORKSPACE_ROOT`.
- `running`: inspect bounded node logs and gateway authentication outcomes. Internet, database,
  node-agent, and peer access must remain unavailable.
- `result_collection_failed`: validate `/workspace/.coire/result.json` is one bounded JSON object.
- `timed_out`: increase the per-run limit only through the typed request; do not weaken node-wide
  limits.
- orphan alert: preserve the audit event and correlated node logs. Reconciliation kills only
  containers carrying Coire's managed run labels.

## Deploy and roll back

Build, scan, and publish both `coire-agent` and `coire-run-relay`. Supply their immutable digests as
`COIRE_RUN_AGENT_IMAGE` and `COIRE_RUN_RELAY_IMAGE` while running `apps/coire-node/install.sh`, then
pull those exact references on each Studio before enabling submissions. Create
`/opt/coire/workspaces/<opaque-ref>/.coire/request.json` as the platform user; callers never supply a
host path.

To roll back, stop new submissions, kill active runs, wait for reconciliation to report no managed
containers, deploy the prior node wheel/plist with the prior two image digests, and restart
`com.coire.node`. Database migration 0011 can be downgraded only after all run/token/command rows
are no longer needed. Never remove the sandbox slice or widen network access as a rollback.

## Credential response

Run-token plaintext exists only between minting and the authenticated node create call. If it is
suspected exposed, kill the run; revocation is immediate. Rotate the node credential separately if
the scheduler-to-node boundary is implicated. Do not copy tokens, container inspect output, or
workspace contents into tickets or audit detail.
