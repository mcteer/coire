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
Raw and converted files remain under the configured Studio model store; DBOS metadata remains in
Postgres. See [`docs/runbooks/acquisition.md`](../../docs/runbooks/acquisition.md).
