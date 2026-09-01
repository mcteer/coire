# Core compose deployment

`coire-up` resolves `coire-core` through UniFi DNS and records the observed bind address in the
gitignored `.env`. Host port 8180 (forwarded to nginx 8080) and OTLP 4317 bind only to that
control address. Override with `COIRE_CONTROL_PORT` or `COIRE_CONTROL_BIND_ADDRESS` only during a
reviewed recovery; never use `0.0.0.0`.

Secrets are materialised from Keychain under `~/.coire/secrets` and mounted as files. Use
`coire-down` to remove them. The integration override creates a shared control network and an
internal Studio-only data network; core is deliberately absent from the latter.

Runtime configuration is supplied through `COIRE_` environment variables and Keychain-sourced
compose secrets. Gateway tuning variables and operational procedures are documented in
[`docs/runbooks/gateway.md`](../../docs/runbooks/gateway.md). Do not put credentials in this file,
`.env`, an image, or a compose environment block.

The local observability stack is enabled in the same project. Grafana is reachable only through
`/grafana/` and uses the Keychain-sourced admin token; Prometheus, Alertmanager, Loki, and Tempo
have no host ports. `COIRE_METRIC_RETENTION`, `COIRE_LOG_RETENTION`, and
`COIRE_TRACE_RETENTION` default to seven days. The default hard ingestion envelope is 32 GB
for Prometheus plus 0.05 MB/s for Loki and 50,000 B/s for Tempo (92.48 GB over seven days);
`COIRE_METRIC_RETENTION_SIZE`, `COIRE_LOG_INGESTION_MBPS`, and
`COIRE_TRACE_INGESTION_BPS` tune those ceilings. Node health behavior is controlled by
`COIRE_NODE_FAILURE_THRESHOLD` (3), `COIRE_NODE_RECOVERY_THRESHOLD` (5),
`COIRE_NODE_DEGRADED_THRESHOLD` (3), and `COIRE_NODE_OBSERVATION_FRESHNESS_S` (45). Node agents
sample on `COIRE_NODE_COLLECTION_INTERVAL_S` (5 seconds) and back off optional sampling when its
budget is exceeded. Operational telemetry never captures request or response content. See the
[`observability` runbook](../../docs/runbooks/observability.md) for diagnosis and rollback.
When cloudflared is deployed, set `COIRE_TUNNEL_HEALTH_URL` to its internal readiness URL;
the API then emits `coire_tunnel_up{tunnel="primary"}` without exposing a tunnel port.

Prometheus, Alertmanager, Loki, Tempo, and Grafana use pinned upstream source or release artifacts.
Where a published binary has a fixed critical dependency finding, the Dockerfile rebuilds that
exact upstream release with the fixed module; it does not carry a Coire code fork. Production
images remain non-root and read-only with one writable named volume per stateful service.
