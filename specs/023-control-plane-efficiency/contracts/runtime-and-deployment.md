# Runtime and deployment contracts

## Wire compatibility

- `/ready`: existing ReadyResponse and process liveness; no external checks added.
- `/health`: existing HealthResponse; database failure 503 within three seconds; enabled optional failures degrade; disabled services are not failures. Sanitized categories replace raw database exceptions. Existing authentication/public-summary behavior is preserved.
- Admin console/SSE: existing authenticated models and Last-Event-ID reconciliation; common projection reuse does not cache authorization. Event cadence is separate from inference streaming.
- Capacity: reuse `source=core-control-plane-runtime`; unsupported CPU utilization is null; UI explicitly labels runtime values.
- Run kill: accepted response and immediate committed revocation/audit retained; completion requires confirmed stop or transactional proof no container was created. Unavailable nodes leave pending kill state.

## Settings

Core settings and deploy README define console refresh 2 s, ordinary idle scan max 5 s, failure backoff max 30 s, scheduler shutdown 10 s, and one combined kill scan at 0.25 s. The kill executor reserves concurrency per node equal to admitted `run_concurrency_cap` (1–32), deduplicates in-flight command IDs and remains bounded by configured node count. Reject kill intervals incompatible with five-second end-to-end headroom. Existing active-operation, revocation, node freshness and failover settings are unchanged. Enabled capabilities consistently drive profiles and health expectations. Scheduler receives RUN_AGENT_IMAGE and RUN_RELAY_IMAGE.

## Deployment

- Normal coire-up uses a validated release and pinned images; --build opts into compilation, --no-build remains accepted.
- Install/preflight helper writes only a validated non-secret release manifest and project-owned complete credential generation outside the checkout. Production/test generations cannot alias.
- Password recovery is explicit, preserves rows, and uses protected input without credential-bearing argv/output/logs. Ordinary up never rotates persisted roles.
- Rollback preserves project/volume identity and coordinates role/credential state; changing a file pointer alone does not rotate a role.
- coire-down cleans only known owned materialized credentials after stopping/releasing mounts; no implicit data removal.

## Profiles

Baseline: web/API/scheduler/PostgreSQL/collector/Prometheus/Alertmanager. Optional: mcp, ops, container-management, diagnostics; remote ingress stays separately configured. Baseline exporters never reference absent historical backends. Diagnostics enable Loki/Tempo/Grafana and matching exporters together. Required binds cannot auto-create directories. Every service preserves hardening, network scope and actual health checks, explicit caps and bounded logs.

Historical-telemetry profiles require the explicit constitution/architecture amendment before implementation. Continuous metrics/alerts/audit/local logs remain mandatory; reduced historical coverage is documented.
