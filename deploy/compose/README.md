# Core compose deployment

`coire-up` resolves `coire-core` through UniFi DNS and installs an image-ID-pinned Compose
manifest under `~/.coire/projects/<project>/releases/`. The active release is the `current`
symlink in that project directory. The manifest, copied cluster config and mounted credential
generation survive source checkout changes. Host port 8180 (nginx 8080) and OTLP 4317 bind only
to the control address. Override with `COIRE_CONTROL_PORT` or `COIRE_CONTROL_BIND_ADDRESS` only
during reviewed recovery; never use `0.0.0.0`.
On a clean host, startup pulls only the digest-pinned PostgreSQL and Docker socket proxy
images when missing; first-party images must already be present or be built explicitly.

Secrets are materialised from Keychain in complete generations under
`~/.coire/projects/<project>/secrets/` and mounted as files. A missing late Keychain item leaves
the current release untouched. `coire-up` checks existing PostgreSQL data with the candidate
credential from the application network before migrations or replacement. If the persisted role
differs, `coire-up --recover-db-role` writes a private `pg_dump` under the project's `backups/`,
changes only that database role, then repeats the network check. `--build` explicitly builds
source images; normal startup uses existing images without building. `coire-down` removes
project credentials after the stack stops and preserves volumes unless `--purge` is confirmed.

Optional `coire-openai-api-key` and `coire-anthropic-api-key` Keychain items are mounted only
into coire-api; absent items stage empty files. `COIRE_PROVIDER_CHAT_ENABLED` defaults false.
See [the frontier Chat runbook](../../docs/runbooks/frontier-chat.md) for bounded acceptance,
publication and rollback.
The integration override creates a shared control network and an
internal Studio-only data network; core is deliberately absent from the latter.

The lean profile starts PostgreSQL, API, web, scheduler, socket proxy, collector, Prometheus and
Alertmanager. `COMPOSE_PROFILES=ops,mcp` enables the ops harness and MCP service;
`COMPOSE_PROFILES=diagnostics` adds Loki, Tempo and Grafana, and switches the collector to its
bounded historical exporters. Combine profiles with commas. The default collector never targets
disabled history backends. The `ops` profile requires the `coire-ops-service-token` Keychain
item; lean and diagnostics-only profiles do not require it. Prometheus retains 48 hours or
2 GiB by default. The optional
`COIRE_METRIC_RETENTION` and `COIRE_METRIC_RETENTION_SIZE` change these caps; historical log and
trace retention use `COIRE_LOG_RETENTION` and `COIRE_TRACE_RETENTION` (48 hours by default).
`COIRE_STATE_ROOT` and `COIRE_SECRETS_BASE` change the per-project state location for isolated
test deployments. `COMPOSE_PROJECT_NAME` must be a single validated project component.

`COMPOSE_PROFILES=chat-files` starts the private CPU file worker. It requires the
`coire-file-worker-service-token` Keychain item created by `scripts/coire-secrets-init.sh`;
the generated `file_worker_service_token` secret is mounted only in the scheduler and worker.
Only those services join the internal `coire-file-processing` network. The worker has no
published port, database mount, model weights, or harness. It reads `coire-chat-originals`
read-only and writes `coire-chat-derived`; both are private named volumes. The image seeds
non-root ownership for its writable volume. `COIRE_FILE_WORKER_TIMEOUT_SECONDS` defaults to
30 and is capped at 30 seconds; one conversion runs per worker process under 512 MiB/1 CPU.
The profile remains opt-in while durable dispatch, blob cleanup and visual serving
are unfinished. `COIRE_CHAT_ENABLED` remains `false` during this stage.
The API also mounts `coire-chat-originals` for generated-key upload and owner-scoped download;
the worker sees that same volume read-only. Original admission limits individual files to
10 MiB and reserves worst-case derived bytes against 50 MiB/conversation and 500 MiB/owner.
Nginx permits an 11 MiB multipart envelope only on the native Chat upload route. Queued jobs
remain pending until the scheduler dispatcher and result publication path are connected.
The API accepts bounded `COIRE_CHAT_UPLOAD_MAX_BYTES` (10 MiB),
`COIRE_CHAT_CONVERSATION_QUOTA_BYTES` (50 MiB), `COIRE_CHAT_OWNER_QUOTA_BYTES`
(500 MiB), `COIRE_CHAT_DERIVED_JOB_MAX_BYTES` (32 MiB),
`COIRE_CHAT_EXTRACTED_TEXT_MAX_BYTES` (1 MiB), `COIRE_CHAT_PDF_MAX_PAGES` (50),
`COIRE_CHAT_UPLOAD_MAX_PIXELS` (20 million), `COIRE_CHAT_NORMALIZED_MAX_PIXELS`
(4 million), and `COIRE_CHAT_NORMALIZED_MAX_SIDE` (2048). These settings can lower
the API admission limits; the CPU worker retains the same fixed upper bounds.
`COIRE_CHAT_PURGE_DEADLINE_HOURS` and `COIRE_CHAT_EVENT_RETENTION_HOURS` are capped
at 24 hours. The worker and scheduler share `COIRE_FILE_WORKER_TIMEOUT_SECONDS`
(1–30 seconds); the worker stays at one active conversion, 512 MiB, one CPU and
64 processes. Larger uploads remain blocked by nginx even if an API setting is
misconfigured.

Runtime configuration is supplied through `COIRE_` environment variables and Keychain-sourced
compose secrets. Gateway tuning variables and operational procedures are documented in
[`docs/runbooks/gateway.md`](../../docs/runbooks/gateway.md). Do not put credentials in this file,
`.env`, an image, or a compose environment block.

Native Chat is gated by `COIRE_CHAT_ENABLED` (default `false` while feature 014 is incomplete).
See [native Chat operations](../../docs/runbooks/chat-web-ui.md) for turn inspection,
Stop, parser alerts, private-file purge, diagnostics and visual rollback.
The experimental inline PNG `/v1` visual route is separately gated by
`COIRE_GATEWAY_INLINE_VISUAL_ENABLED` (default `false`). Keep it disabled until temporary
normalization, asset verification and the visual gateway acceptance in feature 014 are complete.
Before enabling it, set `COIRE_CHAT_PUBLIC_ORIGIN` to the exact HTTPS browser origin; local
development may use `http://localhost` or `http://127.0.0.1` with an optional port. A missing
origin refuses browser writes. See [`docs/runbooks/chat-web-ui.md`](../../docs/runbooks/chat-web-ui.md).
`COIRE_CHAT_OUTPUT_TOKENS` (default 1024, maximum 4096) bounds each text generation and is
reduced to at most one quarter of a selected model's context window. Persisted native event
retention uses `COIRE_CHAT_EVENT_RETENTION_HOURS` (default and maximum 24).

Identity requires `CLOUDFLARE_ACCESS_ISSUER` (the exact team issuer) and
`CLOUDFLARE_ACCESS_AUDIENCE`. Seed `coire-bootstrap-admin-email` in Keychain by running
`COIRE_BOOTSTRAP_ADMIN_EMAIL=you@example.com scripts/coire-secrets-init.sh`; it creates the first
local admin row but is never itself an authenticator. JWKS cache/leeway defaults are controlled by
`CLOUDFLARE_JWKS_TTL_S` (300) and `CLOUDFLARE_JWT_LEEWAY_S` (60). The legacy admin bearer is disabled
unless the explicit rollback/test-only `IDENTITY_LEGACY_ADMIN_ENABLED` switch is set; production
compose does not set it.

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
`CONSOLE_SNAPSHOT_INTERVAL_S` (2) bounds common admin snapshot refreshes independently of
instance-event replay. `SCHEDULER_IDLE_SCAN_MAX_S` (5) and
`SCHEDULER_FAILURE_BACKOFF_MAX_S` (30) cap ordinary empty-queue and failure backoff;
`SCHEDULER_SHUTDOWN_TIMEOUT_S` (10) bounds cleanup. `RUN_KILL_POLL_INTERVAL_S` (0.25,
maximum 0.5) is the separate safety scan and must not inherit ordinary backoff.
`MCP_ENABLED`, `OPS_ENABLED`, and `DIAGNOSTICS_ENABLED` describe which optional capabilities
the deployment starts and therefore which health dependencies it expects.
Sharding uses `LINK_PROBE_INTERVAL_S` (30), `LINK_PROBE_FRESHNESS_S` (120),
`LINK_FAILURES_BEFORE_DOWN` (2), `LINK_SUCCESSES_BEFORE_UP` (3),
`SHARDING_ALLOW_RING_FALLBACK` (true), `SHARDING_START_TIMEOUT_S` (600), and
`SHARDING_PORT_RANGE` (`9600-9699`). The complete MLX-generated JACCL and ring hostfiles are
configured with `SHARDING_JACCL_HOSTFILE` and `SHARDING_RING_HOSTFILE`; latency is telemetry,
never an admission threshold. See [`docs/runbooks/sharded-serving.md`](../../docs/runbooks/sharded-serving.md).
Raw and converted files remain under the configured Studio model store; DBOS metadata remains in
Postgres. See [`docs/runbooks/acquisition.md`](../../docs/runbooks/acquisition.md).

Harness limits use `HARNESS_RETRY_LIMIT` (2), `HARNESS_TOOL_OUTPUT_BYTE_CAP` (16384),
`HARNESS_SUMMARY_THRESHOLD` (0.8), and `HARNESS_EVALUATION_PASS_SCORE` (0.8). The user and ops
harnesses are separate images; only the ops image contains an admin client. See
[`docs/runbooks/agent-harness.md`](../../docs/runbooks/agent-harness.md).

The core-only ops harness uses `OPS_SERVICE_URL` (`http://coire-ops:8003`), `OPS_API_URL`
(`http://coire-api:8000`), `OPS_GATEWAY_URL` (`http://coire-api:8000/v1`), and the registry UUID in
`OPS_MODEL_ID`. Confirmation authority expires after `OPS_CONFIRMATION_TTL_S` (300; never more
than five minutes). Session liveness uses `OPS_SESSION_HEARTBEAT_S` (10) and
`OPS_SESSION_STALE_S` (30); internal/model calls use `OPS_REQUEST_TIMEOUT_S` (120). The
`ops_service_token` compose secret is a dedicated read/propose credential mounted only in
`coire-api` and `coire-ops`; it is not an admin credential and cannot confirm a proposal.
Set the non-secret registry UUID as `COIRE_OPS_MODEL_ID` (see `.env.example`) before bring-up, and
provision `coire-ops-service-token` with `scripts/coire-secrets-init.sh`. Operational procedures
are in [`docs/runbooks/coire-ops.md`](../../docs/runbooks/coire-ops.md).

Studio container orchestration uses `RUN_CONCURRENCY_CAP` (3),
`RUN_DEFAULT_MEMORY_BYTES` (4 GiB), `RUN_MAX_MEMORY_BYTES` (16 GiB),
`RUN_DEFAULT_NANO_CPUS` (2 CPUs), `RUN_DEFAULT_PIDS_LIMIT` (256),
`RUN_DEFAULT_TIMEOUT_S` (900), `RUN_MAX_LOG_BYTES` (8 MiB),
`RUN_MAX_RESULT_BYTES` (4 MiB), `RUN_TOKEN_TTL_S` (1200),
`RUN_WORKSPACE_ROOT` (`/opt/coire/workspaces`), `RUN_DOCKER_SOCKET`
(`/var/run/docker.sock`), and `RUN_GATEWAY_URL` (`http://coire-core.lab:8180/v1`).
`RUN_AGENT_IMAGE` and `RUN_RELAY_IMAGE` have no default and must be release-image references
pinned by digest. The relay caps each request with `RUN_RELAY_REQUEST_BYTES` (2 MiB). Never use
a tag for either runtime image.

MCP coding uses `MCP_SOURCE_HOSTS` (comma-separated reviewed HTTPS Git hosts; default
`github.com`), `MCP_WORKSPACE_MAX_BYTES` (512 MiB),
`MCP_WORKSPACE_PREPARE_TIMEOUT_S` (120), `MCP_ARTIFACT_RETENTION_HOURS` (168, maximum
720), and `COIRE_MCP_RUN_TIMEOUT_SECONDS` (900, range 10–900). The last setting caps the
Studio run; the MCP call itself has a 20-minute ceiling. Each tool call uses a fresh Studio clone; branch bundles stay on the Studio and are
streamed through the owner-scoped API. See
[`docs/runbooks/mcp-server.md`](../../docs/runbooks/mcp-server.md).

## Control-plane failover

Failover is disabled unless the three members are provisioned with a current, core-signed snapshot
and distinct Keychain-sourced secrets. The core publishes only the public verification material and
the approved resident-model roster in `FAILOVER_SNAPSHOT_PATH` (default
`/opt/coire/failover/snapshot.json`); a Studio treats a missing, expired, or invalid snapshot as an
outage. Never place private keys, relay tokens, API keys, or a snapshot in the repository.
Provision the core Ed25519 public key separately as `FAILOVER_CORE_PUBLIC_KEY` on each node and
`COIRE_FAILOVER_CORE_PUBLIC_KEY` for compose. A snapshot cannot declare its own trusted key.
Core also needs `COIRE_FAILOVER_EDGE_A_PUBLIC_KEY` and `COIRE_FAILOVER_EDGE_B_PUBLIC_KEY`.
When `COIRE_FAILOVER_MEMBER_NAME=coire-core`, `coire-up` reads `coire-failover-peer-key` from
the login Keychain (or `COIRE_SECRET_FAILOVER_PEER_KEY` in its CI mode), mounts it only into
`coire-api`, and publishes the ready, published registry roster every 30 seconds or sooner.
Studios fetch and verify that snapshot over the signed peer route; a Studio node agent that
starts before the first snapshot keeps retrying without a restart.

Provision `coire-failover-peer-key` in the System Keychain on each member and
`coire-failover-relay-token` separately on each Studio. The node agent reads these at startup;
they are not node registration tokens and must not be reused for any other route. The relay token
value must match on both Studios because either frontend may relay to the other's node listener.
It is mounted only into the failover frontend as `failover_relay_token`. The release job supplies the exact
digest-pinned `COIRE_FAILOVER_IMAGE` to the Studio compose deployment.
On each Studio run `deploy/compose/coire-studio-failover-create` after setting the image and
both relay URLs. It reads the relay credential from the System Keychain, writes the
Compose file secret under `/opt/coire/secrets`, and creates only the failover container.
`coire-node` starts and stops that precreated container according to its election role.
Set `COIRE_FAILOVER_LOCAL_RELAY_URL` and `COIRE_FAILOVER_PEER_RELAY_URL` to the local and
other Studio's control-listener DNS names (port 9400), reachable from the frontend container.
An unset address refuses inference. Container loopback cannot reach the host node agent.

Configuration defaults are
`FAILOVER_SNAPSHOT_MAX_AGE_S=120`, `FAILOVER_LEASE_TTL_S=15`,
`FAILOVER_ELECTION_INTERVAL_S=2`, `FAILOVER_PROMOTION_THRESHOLD_S=15`,
`FAILOVER_DEMOTION_THRESHOLD_S=45`, and `FAILOVER_DRAIN_TIMEOUT_S=30`; demotion must stay longer
than promotion. Set `FAILOVER_MEMBER_NAME` to this host's name (`coire-core`, `coire-edge-a`, or
`coire-edge-b`) so it polls the other two; leave it empty to keep the poller off. A beat slower
than `FAILOVER_HEARTBEAT_LATENCY_BUDGET_MS` (default 50) is degraded. Three missed beats mark a
peer unreachable. Changing any is a reviewed
recovery operation. See `docs/runbooks/control-plane-failover.md` for activation and break-glass.
