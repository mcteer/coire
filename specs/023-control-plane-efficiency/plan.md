# Implementation Plan: Quiet, reliable control plane

**Branch**: `feat/023-control-plane-efficiency` | **Date**: 2026-09-26 | **Spec**: [spec.md](spec.md)
**Issue**: #24 | **PR base**: `feat/012-coire-ops-confirmed-mutations` at `eb3a362`
**Stage**: Implementation and validation. The user resumed work after the requested planning pause.

## Summary

One PR addresses deployment faults, idle work, misleading capacity presentation, and run-kill correctness defects found during planning. Keep PostgreSQL, DBOS, separate API/scheduler/web containers and Studio boundaries. Implement stable releases, complete secret generations, truthful dependency health, bounded worker loops, independent kill execution, shared console snapshots and optional support-service profiles.

The seven application and monitoring services in the lean baseline are web, API, scheduler,
PostgreSQL, collector, Prometheus and Alertmanager. The Docker socket proxy remains a separate
baseline service for scheduler's bounded container management. Remote ingress is additional
where used; ops, MCP and historical diagnostics are optional. The changed observability policy
was approved through constitution v2.0.0 and ADR-0007 before implementation.

## Technical Context

- Python 3.13, strict TypeScript/React 18, Bash 3.2 entrypoints, existing static Go health probe.
- Existing FastAPI, SQLAlchemy/asyncpg, Pydantic v2, DBOS **2.24.0**, OTel and Compose. No new runtime package or queue service planned.
- Existing PostgreSQL command/state/audit tables; project-owned release/config/secret generations outside Git. A historical observability revision must be linked into the current Alembic chain so the live database can upgrade without stamping or deleting data.
- pytest unit/contract/local integration, Vitest, image policy/scans and generated-contract checks.
- Target: ARM64 24 GiB M4 Pro Mini; no remote Studio mutation or model loading.
- Goals: one common console projection per two-second window; ordinary idle queue pickup within five seconds; responsive-node kill within five seconds; dependency health within three seconds.
- Existing active workflow polling, auth/revocation, lease, freshness and failover bounds remain unchanged.
- Single PR per explicit user direction, overriding normal size/splitting guidance. Separate commits by concern.

## Constitution Check

| Principle | Design status |
|---|---|
| I — Bare engines | Complies; engine behavior and placement unchanged. |
| II — Host placement | Complies; core containers and existing Studio failover exception retained. |
| II-a — Separate hardened services | Complies; optional services keep separate images and privilege/network boundaries. |
| III — Typed contracts | Complies; retain `/ready` liveness and existing capacity discriminator. Core changes precede clients, with regeneration if needed. |
| IV — Zero trust | Complies; sanitized diagnostics, protected secrets, immediate kill-token revocation, per-request admin authorization before shared data. |
| V — Registry/admin authority | Complies; no change to model acquisition authority. |
| VI — Observability | Complies after constitution v2.0.0, ADR-0007 and dependent-spec reconciliation: baseline retains metrics, alerts, audit and bounded local logs; historical storage is an explicit profile. |
| VII — Spec/test gates | Complies; regression tests precede implementation; local integration uses services/stubs without engines. |

The initial planning check identified a governance conflict. Task T002 closed it before
implementation through constitution v2.0.0, ADR-0007, architecture and affected-spec updates.

## Project Structure

- This directory: specification, plan, research, data model, contracts, quickstart, tasks and handoff.
- `packages/coire-core/`: settings and existing health/console/run contracts.
- `apps/coire-api/src/coire_api/`: shared projections, health, retained probes, run guards and command implementations.
- `apps/coire-api/src/coire_scheduler/`: worker ownership, dispatch, DBOS lifecycle and kill workflow.
- `apps/coire-web/src/`: API stream transport, shared hook, presentation and styles.
- `deploy/compose/`, `deploy/observability/`, `scripts/`, Dockerfiles, runbooks and ADRs.
- Existing unit/contract/integration suites plus focused efficiency/deployment regressions.

## Design

### Stable release and secret lifecycle

Namespace host state by validated Compose project under `~/.coire/projects/<project>/`. Keep its startup lock outside the checkout, so concurrent checkouts share the lock. Reject production/test secret-directory aliasing. Stage and validate every required credential in a new 0700 generation before publishing any path; retain container-readable file modes required by bind mounts. Never overwrite files mounted by live containers. Failure removes only unpublished staging. Require optional credentials only for enabled capabilities.

Materialize release-owned runtime configs, exact image references/digests and a non-secret manifest. Preserve project/volume identity. Required file binds use `create_host_path: false`; reject missing/wrong-type/unexpected-symlink sources. Refuse rewriting a release with different contents. Startup/rollback use release paths, never source-relative runtime mounts. Explicit development builds may reference source only for compilation.

Normal startup uses the installed manifest's local image IDs without pulling or compiling;
`--build` opts into local compilation and `--no-build` stays compatible. Authenticate from the
application network before replacing dependents; for new data, initialize PostgreSQL first,
then authenticate before migrations. Existing-data password mismatch fails sanitized preflight,
never resets data or rotates a role automatically. Add correct URL escaping without a new core
dependency.

Provide an explicit recovery operation to synchronize a drifted PostgreSQL role to the canonical Keychain credential through protected input/local administration, with no password-bearing argv, output or database statement log. Verify SCRAM from the application network before dependent recreation; preserve existing rows. `coire-down` removes all known materialized credentials of a successfully stopped project after mounts release, never arbitrary paths or named volumes. Rollback coordinates credential and persisted-role state deliberately.

### Health, profiles and resources

Preserve `/ready` liveness. Use sanitized `/health` and authenticated database preflight for
dependency/startup health. PostgreSQL failure is 503 within three seconds; expected HTTP
dependencies follow enabled capabilities. Reuse the static Go probe; ordinary liveness runs
every 20 seconds with bounded faster startup checks. Keep nginx's local probe and safety
heartbeats separate. Replace observability executable-version checks with actual HTTP checks.

Selectively restore necessary feature-009 backend configs/images without replacing later Compose/network/auth/failover changes. Baseline collector exports metrics and has no Loki/Tempo destinations. Diagnostics enable corresponding backends/exporters together, with redaction and bounded queues/retries. No external telemetry. Mandatory metrics/alerts/audit/bounded local logs survive without Grafana; historical traces/search are explicitly absent when diagnostics is disabled.

Initial caps to validate: API 512 MiB/1 CPU; scheduler 768 MiB/2 CPUs; PostgreSQL 512 MiB/1 CPU; collector 256 MiB/0.5 CPU; Prometheus 512 MiB/0.5 CPU; Alertmanager 128 MiB/0.25 CPU; web 64 MiB/0.25 CPU. PID bounds 128, scheduler 256; logs 10 MiB × 3. Optional services also receive tested explicit caps. Metrics retain seven days plus a storage-size bound; diagnostics retention is bounded. These are initial ceilings, not measured needs. No automatic global OrbStack limit change; document a separately validated 6 GiB/four-CPU trial.

### Worker ownership and safety

Move acquisition/placement/run/shard/benchmark command executors plus run/shard reconcilers into
a scheduler-owned supervisor. Pass `RUN_AGENT_IMAGE`, `RUN_RELAY_IMAGE`, and the selected
`RUN_GATEWAY_URL` to scheduler. Retain API registry/node/link probes because they supply
admission/status caches; retain API failover publication/election. No status persistence
migration.

A reusable stop-aware helper backs off ordinary empty scans to five seconds, failures to 30 seconds, drains successful work immediately and resets after recovery. Every failure increments metrics; initial failure/recovery and rate-limited summaries preserve evidence without repeated tracebacks. Use new settings rather than slowing shared active-operation poll settings. Keep safety reconciliation/freshness cadence intact.

One scheduler kill scanner checks pending KILL commands at a fixed 0.25-second cadence and dispatches them into a separate executor lane, excluding KILL from normal execution. It makes one database scan per interval, not separate dispatch and execution scans. Each node has kill capacity equal to `run_concurrency_cap` (the admission bound, at most 32), so every admitted run on a responsive node can start termination without waiting behind another run; tasks are deduplicated by command ID and globally bounded by node count times that cap. A failed/unreachable node cannot consume another node's capacity. Preserve the four-second node-call timeout and prove total responsive-node latency below five seconds, including concurrent same-node kills. Ordinary idle scanner backoff reduces enough polling to offset this safety scan. Do not share ordinary asyncio events across DBOS's workflow thread/event loop.

Remove `finally` success from `run_kill_workflow`: unconfirmed termination stays `KILL_REQUESTED` with revoked credentials and retryable work. Confirmed/idempotent termination yields one completed-kill audit. A never-placed run is killed without a node call only when transactional state proves no CREATE can race. Guard transitions/token rotation so CREATE or normal completion cannot revive credentials or overwrite pending/terminal kill state. Preserve deterministic command IDs and idempotent node operations.

Bound partial-startup and shutdown cleanup: stop dispatch, cancel local HTTP/waits, preserve unfinished durable rows, stop DBOS, then dispose connections. Test loop-safe pool/session ownership between scheduler and DBOS; isolate factories if needed, never pass checked-out connections across loops. No new broker or notification-trigger migration: the accepted five-second ordinary pickup bound makes LISTEN/NOTIFY unnecessary here.

### Console, browser and observability

Separate common projection from HTTP route functions; project ledgers once. A process-local single-flight cache has a two-second TTL, copy-safe data and no periodic work without viewers. Authorize before cache access and cache only common admin read data. Compare meaningful state separately from generated observation timestamps/cursor; preserve sampled health, staleness transitions, budgets and resource changes. First/reconnected viewers receive snapshots; others receive meaningful changes plus keepalives. Propagate failures truthfully and bound disconnect/shutdown cleanup.

Move stream HTTP transport into `src/api/`; shared hook owns visibility/online handling, reader cancellation and jittered retry (one-second base, 30-second maximum, reset after successful data). Hidden pages close streams; returning reconciles with Last-Event-ID. Stabilize snapshot references/render subtrees. Reduce pervasive blur and respect reduced motion/transparency. Token streaming remains independent.

Reuse `CoreHostCapacity.source` and label runtime memory/disk distinctly from the Mac. Unsupported CPU utilization is null, not load-average-derived. No new host exporter or GPU claim.

Add bounded-cardinality metrics for projections/cache/events, worker scans/backoff/errors/task health, startup failures and kill latency/age. Moved work uses `coire.scheduler.*` spans with correlation fields. Exclude routine liveness traces. Add efficiency dashboards and stalled-worker/overdue-kill/dependency-failure alerts; audit remains unsampled. Tracing policy and coverage must match the governance amendment.

## Verification and Rollout

Tests precede code: secret failure atomicity, state isolation, stable-release sources, URL escaping, sanitized health, worker ownership/backoff/shutdown and kill races, cache concurrency/semantic equality, visibility/backoff/cancellation. Local Compose tests preserve seeded rows through credential reconciliation, prove profile/exporter consistency, scheduler restart recovery and blocked-WAIT kill timing. Preserve existing engine-independent acquisition/placement/sharding/run integration.

Run format/lint/types, Python unit/contract and relevant integration, web tests/lint/build, OpenAPI/TS freshness, all supported Compose profile combinations, changed-image build/policy/scans/SBOM gates. Record unavailable gates explicitly rather than marking complete. No actual Studio engine behavior is changed.

After tests and recoverable release preparation, repair only affected local core services. Capture deployment/data state first; reconcile canonical credentials only on proven mismatch. Verify authenticated health and before/after 60-second auth/export error counts, resource/CPU/disk samples. Desktop-frame improvement requires separate reproduction; stopping OrbStack is a planned interruption, not automatic remediation. One PR includes all code/design/tests/evidence/runbook changes.

## Complexity Tracking

Optional historical telemetry is the sole planned policy departure and an explicit prerequisite. Native core migration, SQLite, merged roles, new brokers and host telemetry daemons add migration cost without measured necessity and remain out of scope.
