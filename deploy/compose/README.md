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

Acquisition tuning uses `ACQUISITION_POLL_INTERVAL_S` (2), `ACQUISITION_STUCK_SECONDS` (1800),
`ACQUISITION_PERPLEXITY_TOLERANCE` (0.10), `ACQUISITION_CONVERSION_MEMORY_OVERHEAD` (1.20),
`ACQUISITION_DISK_SAFETY_FRACTION` (0.10), and `ACQUISITION_VALIDATION_FIXTURE_VERSION` (`v1`).

Placement uses `PLACEMENT_DEFAULT_BUDGET_BYTES` (230 GiB), `PLACEMENT_SANDBOX_BYTES` (16 GiB),
`PLACEMENT_HEALTH_FRESHNESS_S` (30), `PLACEMENT_BUSY_DRAIN_TIMEOUT_S` (10),
`PLACEMENT_CPU_SATURATION_PERCENT` (90),
`PLACEMENT_POLL_INTERVAL_S` (1), `PLACEMENT_TTL_INTERVAL_S` (30), and
`PLACEMENT_LEASE_TTL_S` (60). The budget is authoritative; measured resident memory is used
only for drift telemetry. Reducing a budget below current reservations blocks admission but
does not force eviction.
Instance lifecycle uses `INSTANCE_DRAIN_TIMEOUT_S` (30) for bounded graceful drain and
`INSTANCE_EVENT_POLL_INTERVAL_S` (0.5) for persisted SSE replay polling.
Sharding uses `LINK_PROBE_INTERVAL_S` (30), `LINK_PROBE_FRESHNESS_S` (120),
`LINK_FAILURES_BEFORE_DOWN` (2), `LINK_SUCCESSES_BEFORE_UP` (3),
`SHARDING_ALLOW_RING_FALLBACK` (true), `SHARDING_START_TIMEOUT_S` (600), and
`SHARDING_PORT_RANGE` (`9600-9699`). The complete MLX-generated JACCL and ring hostfiles are
configured with `SHARDING_JACCL_HOSTFILE` and `SHARDING_RING_HOSTFILE`; latency is telemetry,
never an admission threshold. See [`docs/runbooks/sharded-serving.md`](../../docs/runbooks/sharded-serving.md).
Raw and converted files remain under the configured Studio model store; DBOS metadata remains in
Postgres. See [`docs/runbooks/acquisition.md`](../../docs/runbooks/acquisition.md).
