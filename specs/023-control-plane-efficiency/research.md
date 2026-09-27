# Research: Control-plane efficiency

## R1 — Source and live deployment differ

Decision: repair deployment through stable release-owned inputs. Running application containers were created September 1; the working base contains feature 020. The live collector retries full-stack destinations while checked-in collector configuration is bootstrap-only. Four stopped backends report file/directory bind mismatches in the mutable checkout.

Measured September 26: approximately 453 MiB container totals, 2.6 GiB OrbStack helper RSS (different accounting), zero swap, follow-up CPU 92.9–94.5% idle. User confirms macOS window-movement lag. This does not prove Coire causes the lag. Alternative rejected: blaming feature 020 or promising unmeasured savings.

## R2 — Persisted PostgreSQL credentials drifted

Read-only checks: API/scheduler/PostgreSQL mount identical password bytes; fresh Settings and parsed URL preserve them; fresh application-network asyncpg auth still fails. Local socket/loopback rules use trust while cross-container connections use SCRAM. Local psql/pg_isready success therefore does not verify the application credential.

Decision: full credential generations plus cross-container preflight and explicit data-preserving recovery. Current coire-up overwrites live files before all inputs are validated; coire-down removes only three of the current credential files. Fix reserved-character database URL encoding too, although it does not explain this instance. Reject volume deletion and `coire-secrets-init --force` as recovery; neither safely reconciles the current role and desired credential.

## R3 — Liveness is intentionally separate

Decision: preserve `/ready` contract; use sanitized `/health` and database preflight for dependencies. Disabled optional capabilities are not failures. Reuse static HTTP probes and test actual backend listeners, replacing version-command checks. Ordinary probe interval increases independently of kill/election/freshness bounds.

## R4 — Worker ownership and concrete kill defects

Decision: move seven orchestration workers to scheduler, retaining API probe/status/failover components. Add scheduler run image settings. Empty ordinary queue backoff caps at five seconds; failures at 30 seconds. This explicitly trades up to five seconds of ordinary pickup latency for fewer scans.

RunCommandExecutor serial WAIT blocks KILL. run_kill_workflow marks KILLED in finally despite submission failure. Guard concurrent CREATE/completion/token rotation as well as adding an independent priority lane and confirmed-success transition. One 0.25-second kill scan produces four idle queries per second, instead of two proposed 0.1-second loops producing twenty; per-node kill concurrency must equal the admitted run cap to avoid same-node queue delay. DBOS 2.24.0 async workflows use another event loop; session/synchronization ownership requires integration coverage.

Alternative: [PostgreSQL NOTIFY](https://www.postgresql.org/docs/17/sql-notify.html) can wake workers after commit while tables remain authoritative. Reconnect/missed-notification and cross-loop work are unnecessary for the accepted initial latency bound; no broker or notification migration is needed.

## R5 — Shared snapshots and accurate capacity

Decision: two-second single-flight projection cache, one ledger read, meaningful-state equality excluding generated envelope observations. Current half-second per-client JSON always differs through timestamps. Preserve real health/staleness and auth boundaries. Hidden pages stop streaming; retries/cancellation are bounded.

Capacity already includes a runtime-source discriminator. Reuse it; show CPU unknown when no utilization measurement exists. No redundant wire model, host exporter or framework rewrite.

## R6 — Profiles need a governance amendment

Decision: seven-service baseline; optional MCP/ops/container-management/diagnostics. Match collector exporters to enabled backends. Selectively recover useful feature-009 resources without replacing later configuration. Continuous metrics/alerts/audit/local logs remain; historical traces/search require diagnostics. Current VI and technology constraints require an explicit update before implementation.

[Compose profiles](https://docs.docker.com/compose/how-tos/profiles/) selectively enable services, while explicit service targeting can start a profiled service. Validate both profile combinations and deployment entrypoints so exporters cannot target absent services.

## R7 — Resource ceilings are not usage

Decision: tested per-service caps, bounded logs and explicit build opt-in; global OrbStack settings unchanged. Its limits are ceilings, not reservations: [OrbStack settings](https://docs.orbstack.dev/settings). A 6 GiB/four-CPU trial is guidance requiring representative validation, not a promised cure for desktop stutter.

Two read-only research agents were used as prescribed by the planning skill. Neither mutated files/services/credentials. No unresolved technical research prevents task generation; the observability governance conflict remains explicit.
