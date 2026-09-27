# Core control plane efficiency and recovery

The core Mini hosts the control plane only. Model engines and user harnesses remain on the
Studios. The lean profile keeps local metrics and alert rules, but omits the ops harness, MCP,
Loki, Tempo and Grafana unless their Compose profiles are selected. This changes historical
coverage: without `diagnostics`, spans are visible only in bounded collector output, and logs
are rotated locally rather than searchable in Loki. Prometheus metrics and Alertmanager rules
remain active in every profile. See [ADR 0007](../adr/0007-lean-control-plane-diagnostics.md).

## Prepare and observe

From a reviewed feature branch, run `deploy/compose/coire-up --build`. The script holds a
project lock, reads Keychain items into a complete new generation, resolves Compose, installs a
manifest pinned to local image IDs, and checks existing PostgreSQL credentials from the
application network. Only after this succeeds does it replace services. A regular restart runs
`deploy/compose/coire-up` and uses already-built images. Run
`COMPOSE_PROFILES=ops,mcp deploy/compose/coire-up --build` if those capabilities are needed.
Use `COMPOSE_PROFILES=diagnostics` only for a bounded historical investigation.

Inspect the current release through `~/.coire/projects/coire/current/compose.json`. Run
`docker compose -p coire -f ~/.coire/projects/coire/current/compose.json ps` and inspect the
authenticated `/health` route through the gateway. `/ready` reports process liveness only;
`/health` checks PostgreSQL and enabled dependencies and hides exception details. Watch the
`CoireSchedulerWorkersMissing`, `CoireRunKillSlow` and `CoireConsoleSnapshotChurn` rules in
Prometheus. The optional Grafana dashboard is **Coire control plane efficiency**. Container
logs rotate at 10 MiB × 3 files per service.

The scheduler owns acquisition, placement, run, shard and benchmark command workers. The API
retains request-serving probes and its registry projection. Idle command scans back off to five
seconds; failed scans to 30 seconds. The kill lane runs one combined scan every 0.25 seconds,
admits up to `RUN_CONCURRENCY_CAP` simultaneous kills per Studio, and gives each node call four
seconds. A kill stays pending until the node confirms it. Inspect `run_command` and
`agent_run` rows, the `coire_run_kill_queue_latency_seconds` metric and correlated `run_id`
logs if an alert fires. Do not mark a pending kill complete by editing its row.

## Database credential drift

`pg_isready` can succeed over the local trust socket even when API network authentication
fails. The release preflight therefore starts a one-shot, non-root migration image on the
project's database network and runs `SELECT 1` using the candidate mounted credential. On
failure, it leaves the running containers and data volume alone. Check which project is active
and whether the Keychain's `coire-postgres-password` is the intended canonical credential.
After reviewing the retained generation, use `deploy/compose/coire-up --recover-db-role`.
It first saves a private custom-format database dump under the project's `backups/`, updates
only the persisted `coire` role password over the local socket, then repeats network preflight.
The secret is passed over stdin, never in a command argument or log. Never remove
`coire-pgdata` to fix an authentication mismatch. `scripts/coire-secrets-init.sh --force`
retains the PostgreSQL item unless `--rotate-postgres` is explicitly included.

The 2026-09 core database predates the acquisition branch and reports Alembic revision
`0005_observability_health`. This release includes that exact historical migration as the
parent of `0005_acquisition_variants`, so normal Alembic upgrade proceeds from the retained
revision. Keep the pre-upgrade custom-format dump until row counts and authenticated health
are verified. Do not manually stamp the revision or drop the old observation columns.

## Rollback and cleanup

Every successful release has a complete manifest, copied cluster config and credential
generation. To roll back, use the preceding `releases/release-*` directory's `compose.json`
with `docker compose -p coire -f <path> up --no-build --pull never -d --wait`; verify `/health`
before running `uv run python scripts/coire-deploy.py select <release-directory>`. Keep the prior release and its mounted
credential generation until the replacement is stable. `coire-down` stops the whole project and
cleans owned credentials; it preserves named volumes unless `--purge` is confirmed.

After replacement health and a data-row check, remove only stopped or orphaned containers
labelled `com.docker.compose.project=coire` whose services are absent from the selected release.
Do not use `docker system prune`, remove named volumes, or touch Studio containers. Compare
`docker ps -a --filter label=com.docker.compose.project=coire` before and after. OrbStack's
global memory and CPU caps are not adjusted by this feature; an ordinary idle host profile
alone cannot prove Coire caused desktop frame lag.
