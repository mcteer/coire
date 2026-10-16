# Core compose deployment

### Distinct native recovery support mode (019, not Core model work)

`UPGRADE_RECOVERY_SUPPORT_JOB_ID`, `UPGRADE_RECOVERY_SUPPORT_GENERATION` and
`UPGRADE_RECOVERY_SUPPORT_SOURCE_SHA256` select the complete original scope for
`coire_api.upgrades.recovery_support` in the ordinary hardened API image. All three
and enabled upgrades are required. The compatibility instance exposes readiness
and authenticated recovery/schema paths, retains the standard credential and
role/scope gates, and runs no ordinary API lifespan workers. Its configured tuple
supplies no native authority or recovery budget. Other failures and API concerns
are unavailable. The frozen incumbent remains intact. Isolated acceptance uses
loopback publication and existing authenticated SSH transport; production network
or firewall policy must not be widened to reach this endpoint.

`UPGRADE_RECOVERY_CORE_API_PORT` optionally selects the separately scoped Core
compatibility endpoint on the Studio's existing `CORE_CONTROL_HOST`. It requires
enabled upgrades and a Studio identity; its default is unset, preserving
`CORE_API_PORT`. Immutable support observer/startup/guard/supervisor processes use
this port. Fixed Source node and guard launch producers remove it from the child
environment so ordinary lease, workload and upgrade traffic keeps the incumbent
Core endpoint. No request may supply a URL, hostname or port as effect authority.

`UPGRADE_RECOVERY_SCHEMA_ADMIN_TOKEN_FILE` defaults to
`/run/secrets/upgrade_recovery_schema_admin_token`. The fixed one-shot
`coire_api.upgrades.recovery_schema_bootstrap` utility reads this Keychain-sourced
mounted admin secret and invokes the ordinary authenticated admin API in process.
Its bounded strict stdin command is `observe` or `prepare` for an exact original
Studio failure. It exposes no listener and accepts no SQL, credential or budget
in its command. The preparation leaves the original operation and Alembic history
unchanged; it cannot be used as execution authority. Unknown schema preimages,
partial protocols, invalid credentials and revoked administrator roles refuse.

`UPGRADE_RECOVERY_SUPERVISION_FD` is internal to the fixed Studio fixture
supervisor handback. The trusted producer passes an already held private
descriptor to `coire_node.expired_recovery_source_supervisor`; its child retains
the same kernel lock after the parent closes its descriptor. It requires enabled
upgrades, a Studio node and the scoped recovery selector. A configured integer or
portable receipt supplies no ownership. This is neither a Core deployment setting
nor a file descriptor that operators should manufacture. Source node/guard children
receive no recovery selectors or supervision descriptor.

`UPGRADE_RECOVERY_SUCCESSION_ID` optionally selects a distinct uppercase
ULID successor under `UPGRADE_RECOVERY_ID`; missing/equal parent ids refuse
configuration. `UPGRADE_RECOVERY_SUCCESSION_MODE` defaults to `resume`
(existing exact scoped claim); the only other value is explicit `initial`, which
requires both selectors and exact verified original startup/predecessor. Initial
mode composes passive authenticated grant delivery with the actual-exclusion
controller and refuses retained claims rather than recapturing their epoch.
These selectors are not grants, deadline overrides or permission to acquire a
replaced PID. Core explicit state and actual lifetime/physical gates remain
mandatory; missing/corrupt/mismatched context never changes selected mode or falls
back to legacy. Trusted initial observation/launch remains incomplete. Do not
deploy partial 019 support.

`UPGRADE_RECOVERY_ID` is an optional 26-character uppercase ULID selector
for the installed Studio-only `coire_node.upgrade_guard` support mode. Leave it
unset for normal legacy guard operation. It is metadata, not authorization or a
budget override. The supported recovery preparation producer must first retain
verified immutable `expired-recovery/<id>/startup.json` under the configured
bootstrap root. Missing/corrupt/mismatched context, disabled upgrades or failed
fresh Core authority refuse; support exit never falls back to legacy recovery or
its expired deadline. Credentials continue to come from the existing Keychain
node item. Do not set this on Core, edit sealed Source files, manually manufacture
startup/claim records, or treat an unready holding controller as serving proof.
Installation/launch and complete Source handback/native acceptance gates remain
required before deploying this mode; this setting alone must not activate it.

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
`COIRE_CHAT_DEFAULT_MODEL_ID` may name an administrator-registered model UUID to place first
in the plain Chat picker. It is considered only while published and entitled; Code mode and
saved conversation selections retain their own model choice. Keep the provider key in Keychain,
not `.env.local` or Compose environment variables.
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
the generated `file_worker_service_token` secret is mounted in the scheduler, worker and API.
Those services join the internal `coire-file-processing` network. The API reads the worker's
authenticated upgrade inventory so an active or unknown conversion prevents Core activation.
The additional API membership and credential mount were explicitly approved for feature 019.
The worker has no
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

Native dataset analysis reads private registered sources from `COIRE_TRAINING_INPUT_API_URL`
(default `http://coire-core.lab:8180`). The node supervisor verifies the origin against
`COIRE_CORE_CONTROL_HOST`, requires both its Keychain-sourced node credential and an expiring
analysis-bound header grant, verifies source size/digest, and supplies the CPU worker only
generated local IDs. The worker receives no source grant or administrator credential.
Dataset uploads reserve aggregate spool/source/index quota before multipart parsing. They require
a finite Content-Length, refuse content encoding/transfer encoding, and use the configured
analysis timeout as the whole-upload deadline. Keep `COIRE_TRAINING_ENABLED=false` until all
feature-016 capability and release gates pass.

Native Chat is gated by `COIRE_CHAT_ENABLED` (default `false` while feature 014 is incomplete).

Image admission is reserved behind `COIRE_IMAGE_ENABLED` (default `false`). These values
are wired into the API and applicable scheduler settings. The `coire-blobs` volume is mounted
by the API for private transfer and by the scheduler for verified publication; the isolated
file worker uses dedicated `images` subpaths of its existing
original/derived mounts. Basic job, owner input, and output routes now exist, but advanced
generation modes, measured chat coexistence and required operator gates remain incomplete.
Keep admission disabled until the parent feature acceptance gates pass; see
[`docs/runbooks/image-generation.md`](../../docs/runbooks/image-generation.md).
For development acceptance involving recipe imports, init images, masks, or control
inputs, start the file worker with `COMPOSE_PROFILES=image-files` alongside
`COIRE_IMAGE_ENABLED=true`. The admission flag alone does not start this profile.
The file worker reads image inputs from `FILE_WORKER_IMAGE_INPUT_ROOT`
(`/opt/coire/chat/originals/images`), a read-only namespace containing generated UUID
names. Its authenticated internal routes accept an ID, size and SHA-256, never a path.
Recipe imports parse metadata only; generation inputs normalize to private PNGs under
`FILE_WORKER_IMAGE_OUTPUT_ROOT` (`/opt/coire/chat/derived/images`). The API and scheduler
share these mounts and keep generation input admission disabled until the advanced-mode
pipeline and operator gates pass.

| Compose-facing setting | Default | Maximum |
| --- | ---: | ---: |
| `COIRE_IMAGE_GENERATION_INPUT_MAX_BYTES` | 10 MiB | 10 MiB |
| `COIRE_IMAGE_RECIPE_INPUT_MAX_BYTES` | 64 MiB | 64 MiB |
| `COIRE_IMAGE_RECIPE_METADATA_MAX_BYTES` | 64 KiB | 64 KiB |
| `COIRE_IMAGE_OUTPUT_MAX_BYTES` | 64 MiB | 64 MiB |
| `COIRE_IMAGE_MAX_OUTPUTS` | 4 | 4 |
| `COIRE_IMAGE_PENDING_PER_OWNER` | 4 | 4 |
| `COIRE_IMAGE_PENDING_GLOBAL` | 32 | 32 |
| `COIRE_IMAGE_DAILY_OUTPUTS_PER_OWNER` | 100 | 100 |
| `COIRE_IMAGE_OWNER_STORAGE_QUOTA_BYTES` | 5 GiB | 5 GiB |
| `COIRE_IMAGE_GLOBAL_STORAGE_QUOTA_BYTES` | 50 GiB | 50 GiB |
| `COIRE_IMAGE_DISK_SAFETY_FLOOR_BYTES` | 2 GiB | Cannot be lowered |
| `COIRE_IMAGE_WORKER_IDLE_TTL_S` | 900 s | 86,400 s |
| `COIRE_IMAGE_EDIT_JOB_TIMEOUT_S` | 7,200 s | 14,400 s |
| `COIRE_IMAGE_EVENT_RETENTION_HOURS` | 24 h | 24 h |
| `COIRE_IMAGE_OUTPUT_RETENTION_HOURS` | Unset: owner deletion only | Optional 1–8,760 h |
| `COIRE_IMAGE_COMPATIBLE_WAIT_S` | 90 s | 90 s |
| `COIRE_IMAGE_CANCEL_GRACE_S` | 5 s | 5 s |
| `COIRE_IMAGE_PROMPT_CACHE_MAX_BYTES` | 256 MiB | 256 MiB |
| `COIRE_IMAGE_CONTROL_CACHE_MAX_BYTES` | 256 MiB | 256 MiB |
| `COIRE_IMAGE_CLASSIFIER_MEMORY_BYTES` | 1 GiB | 4 GiB |

The API exposes current input/output ceilings, owner quota, pending/daily limits and
output-retention policy through authenticated `GET /api/v1/images/models`, even
when generation is disabled. The Images form displays them before submission and
refuses generation when the policy is unavailable. Event retention is separate
from image retention. Setting `COIRE_IMAGE_OUTPUT_RETENTION_HOURS` opts into
expiry from successful publication time, including already stored outputs: review
the policy before restarting the API. Blank/unset preserves owner-deletion-only
retention. Every maintenance pass tombstones at most 25 expired outputs, with a
required `image.output.expire` audit; existing purge work removes bytes and only
then releases quota. Database/audit failures roll back that tombstone batch and
raise the existing image-purge failure signal. Inputs never inherit output expiry.

Studio agents push completed PNGs to `COIRE_IMAGE_TRANSFER_API_URL` (default
`http://coire-core.lab:8180`). Keep that setting on the node pointed at the
trusted core ingress. The API requires the Keychain-sourced node bearer and a
five-minute grant scoped to the job, attempt, fence and output index. It stores
verified bytes privately under `COIRE_IMAGE_BLOB_ROOT` and returns a receipt
before the Studio may clean its scratch copy.

On each native Studio node, `COIRE_NODE_IMAGE_WORKER_PORT` defaults to `9600`.
It is reserved for the one resident image worker's authenticated loopback
control app and must stay outside `COIRE_NODE_ENGINE_PORT_RANGE` (default
`9500-9599`). The node refuses a collision; this setting opens no external
listener.

Nginx allows a 65 MiB multipart body only at `/api/v1/image-inputs` to fit a 64 MiB
recipe PNG plus framing. The API enforces the purpose-specific 10 MiB generation and
64 MiB recipe file caps. The internal raw PNG transfer path is limited to 64 MiB. Other
API paths retain their existing body bounds.

See [native Chat operations](../../docs/runbooks/chat-web-ui.md) for turn inspection,
Stop, parser alerts, private-file purge, diagnostics and visual rollback.
The experimental inline PNG/JPEG/WebP `/v1` visual route is separately gated by
`COIRE_GATEWAY_INLINE_VISUAL_ENABLED` (default `false`). It requires the `chat-files` Compose
profile and its Keychain-sourced worker token: the API stages generated-key originals, the
private CPU worker normalizes them, and expiry erases worker outputs before originals. Keep the
flag disabled until temporary-job integration and visual gateway acceptance in feature 014 pass.
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
For administrator management inside Chat, set `COIRE_OPS_MODEL_SOURCE=anthropic` and use the
curated Sonnet registry UUID as `COIRE_OPS_MODEL_ID`. Coire Ops stays on internal networks and
uses its scoped token to call the API's bounded provider relay; the Anthropic Keychain secret
remains mounted only in coire-api. The default source is `studio`.

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
On each Studio run `deploy/compose/coire-studio-failover-create` after setting the image,
`COIRE_FAILOVER_MEMBER_NAME` to that Studio's exact name, and both relay URLs. The script
rejects execution on Core or a mismatched host. Provision separate System Keychain items
`coire-failover-monitor-token` (edge readiness only) and `coire-failover-lifecycle-token`
(local in-flight accounting only), distinct from the relay credential and each other.
It writes these three Compose secrets under the existing 0700 `/opt/coire/secrets`
directory and creates only the failover container. Node startup loads the same items;
missing lifecycle authentication is treated as unknown/busy accounting, never proven idle.
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

Core also reads `coire-failover-monitor-token` from its login Keychain; armed `coire-up`
requires it and mounts it only into `coire-api`. Monitor requests carry
`X-Coire-Failover-Monitor`; the relay and local lifecycle credentials cannot substitute for
it. Browser surfaces require approved Access assertions, including direct-origin assets.
Do not broaden Access policies or network bindings without separate approval.

Additional settings (runtime names; secret values must not be exported):

| Runtime setting | Default / Compose input |
| --- | --- |
| `FAILOVER_MEMBERSHIP_EPOCH` | `1`; `COIRE_FAILOVER_MEMBERSHIP_EPOCH` on Core/frontend, matching node settings |
| `FAILOVER_ACCESS_SUBJECTS` | `{}`; `COIRE_FAILOVER_ACCESS_SUBJECTS`, JSON Access subject-to-registered-user-email map on Core only; empty means no eligible degraded users |
| `FAILOVER_CONTROL_HOSTS` | fixed member map to `coire-core.lab`, `coire-edge-a.lab`, `coire-edge-b.lab`; control DNS only, no data-fabric names or raw IPs |
| `FAILOVER_SAFETY_PATH` | empty derives a member-specific file beside the snapshot/proof; never use `/tmp` |
| `FAILOVER_MONITOR_TOKEN` | Keychain/file secret `failover_monitor_token`, readiness scope only |
| `FAILOVER_LIFECYCLE_TOKEN` | per-Studio Keychain/file secret `failover_lifecycle_token`, local lifecycle scope only |

Core's `/opt/coire/failover` is a dedicated durable `coire-failover-state` volume,
seeded with uid/gid 65532 and mode 0700 in the bare API image. It is **not tmpfs**.
Studios keep member safety records (0600), signed snapshots/proofs, and bounded journals
in their persistent host directory; the frontend mount stays read-only. Preserve safety
records and all unacknowledged journal/terminal records during upgrades and rollback.
Missing/corrupt safety state quarantines authority for the maximum prior supported
300-second lifetime plus 1-second uncertainty, even when the new configured TTL is lower.
Only committed-ID, signed Core acknowledgements allow journal compaction.

Protocol-v2 rollout is coordinated, not mixed-version live activation. Drain/disarm both
Studio frontends/controllers first; remove serving proofs but preserve safety and audit
files. Retain the prior deployment/artifacts through the 019-owned update. Update all
participants and frontend schemas/configuration to the matching protocol and trusted
epoch/key set; old unbound overrides, old heartbeats and mismatched epochs fail closed.
Wait out first-boot quarantine where applicable, obtain fresh (at most 120-second) signed
snapshots, then re-arm and validate before restoring edge eligibility. Never extend a
snapshot/override or weaken scopes to bridge a rollout. Rollback must also disarm first;
do not roll back trusted epoch, overwrite safety records or discard audit receipts.
The corrected feature remains unqualified until the required feature-020 release/live
gates pass; configuration or passing unit tests alone do not authorize activation.

Frontend telemetry sets runtime `OTLP_ENDPOINT=http://coire-core.lab:4317`, using the existing
approved Studio control path. `COIRE_DIAGNOSTICS_ENABLED` maps to `DIAGNOSTICS_ENABLED=false`
by default; metrics remain enabled with bounded one-second export deadlines, and diagnostic
traces use a bounded 256-entry queue. Collector loss does not extend serving authority.

Cloudflare deployment/public-edge qualification is deferred by the owner. The Cloudflare LB
manifest is design-only and must not be applied as a provider API payload. Authenticated local
origin qualification remains required; production activation still requires the pending live
resource, recovery and ingress gates in the feature execution record.

## SFT training settings (feature 016 implementation)

Training is default-off. These runtime environment names map directly to fields in
`coire_core.settings.Settings`. Compose uses `COIRE_TRAINING_ENABLED`
to set runtime `TRAINING_ENABLED`; enablement requires the feature's acceptance gates and
compatible nodes. Route implementations do not establish deployed node/scheduler acceptance.
Per-model capabilities may tighten these bounds. Raising a deadline or lowering an evidence
floor beyond the listed protection is rejected by Settings validation.

| Runtime variable | Default |
| --- | --- |
| `TRAINING_ENABLED` | `false` |
| `EVALUATIONS_ENABLED` | `false`; Compose input `COIRE_EVALUATIONS_ENABLED` enables new evaluation admission only |
| `EVALUATION_MAX_ACTIVE_GROUPS`, `EVALUATION_MAX_PENDING_RUNS` | `1`, `100`; active concurrency cannot be increased |
| `EVALUATION_QUEUE_TIMEOUT_SECONDS`, `EVALUATION_TIMEOUT_SECONDS` | `3600`, `900`; bounded acceptance and execution deadlines |
| `EVALUATION_EVIDENCE_RETENTION_DAYS`, `EVALUATION_EVIDENCE_QUOTA_BYTES` | `7`, `1073741824`; private evidence expires independently of immutable scores |
| `TRAINING_DATASET_DIR` | `/opt/coire/training/datasets` (API-owned private original store) |
| `TRAINING_INPUT_API_URL` | `http://coire-core.lab:8180`; Compose input `COIRE_TRAINING_INPUT_API_URL`, reachable by Studios over the existing control fabric |
| `TRAINING_RECIPE_MAX_BYTES`, `TRAINING_YAML_MAX_DEPTH` | `65536`, `16` |
| `TRAINING_DATASET_UPLOAD_MAX_BYTES`, `TRAINING_DATASET_MAX_ROWS` | `268435456`, `1000000` |
| `TRAINING_DATASET_ROW_MAX_BYTES`, `TRAINING_DIAGNOSTIC_MAX_ROWS` | `1048576`, `100` |
| `TRAINING_DATASET_QUOTA_BYTES`, `TRAINING_DATASET_DISK_FLOOR_BYTES` | `21474836480`, `2147483648` |
| `TRAINING_STAGING_RETENTION_S` | `86400` |
| `TRAINING_ANALYSIS_MEMORY_BYTES`, `TRAINING_ANALYSIS_TIMEOUT_S` | `1073741824`, `1800` |
| `TRAINING_MIXTURE_MAX_SOURCES` | `16` |
| `TRAINING_MAX_UPDATES`, `TRAINING_MAX_SEQUENCE_LENGTH` | `100000`, `8192` |
| `TRAINING_MAX_BATCH_SIZE`, `TRAINING_MAX_ACCUMULATION_STEPS`, `TRAINING_MAX_ADAPTER_RANK` | `64`, `64`, `128` |
| `TRAINING_MAX_PENDING_GLOBAL`, `TRAINING_MAX_PENDING_PER_ADMIN` | `8`, `4`; per-admin cannot exceed global |
| `TRAINING_QUEUE_TIMEOUT_S`, `TRAINING_EXECUTION_TIMEOUT_S` | `86400`, `259200` (cumulative active execution) |
| `TRAINING_CHECKPOINT_EVERY_UPDATES`, `TRAINING_CHECKPOINT_KEEP_LAST` | `100`, `3` |
| `TRAINING_CHECKPOINT_QUOTA_BYTES`, `TRAINING_ARTIFACT_QUOTA_BYTES` | `21474836480` per job, `214748364800` per Studio |
| `TRAINING_ARTIFACT_DISK_FLOOR_BYTES` | `21474836480` |
| `TRAINING_EXECUTION_LEASE_S`, `TRAINING_LEASE_RENEW_S` | `30`, `10`; renew before expiry |
| `TRAINING_CANCEL_GRACE_S`, `TRAINING_PAUSE_GRACE_S`, `TRAINING_WATCHDOG_INTERVAL_S` | `5`, `60`, `1` |
| `TRAINING_TRANSFER_GRANT_S`, `TRAINING_MAX_RECOVERY_ATTEMPTS` | `60`, `3` |
| `TRAINING_PROFILE_TTL_S`, `TRAINING_GUARD_INTERVAL_S` | `604800`, `5` |
| `TRAINING_LATENCY_WINDOW_S`, `TRAINING_LATENCY_MIN_SAMPLES` | `300`, `30` per resident target |
| `TRAINING_MEASUREMENT_MIN_REQUESTS`, `TRAINING_MEASUREMENT_PHASE_S` | `100`, `900` per target per baseline/mixed phase |
| `TRAINING_TELEMETRY_FRESHNESS_S`, `TRAINING_CHAT_TTFT_LIMIT_S` | `60`, `1.5` |
| `TRAINING_PROTECTIVE_COOLDOWN_S` | `60` |
| `TRAINING_EVENT_RETENTION_S`, `TRAINING_EVENT_HEARTBEAT_S` | `604800`, `15` |
| `TRAINING_LOG_MAX_BYTES`, `TRAINING_METRIC_PAGE_MAX` | `1048576`, `2000` |
| `TRAINING_LIST_PAGE_DEFAULT`, `TRAINING_LIST_PAGE_MAX` | `25`, `100` |

Evaluation evidence uses the `evaluation-evidence/` namespace inside the existing
API/scheduler `coire-training-data` volume at `/opt/coire/training/datasets`. There
is no additional volume or mount audience. The parent and namespace require mode
0700, evidence files mode 0600, and every run is limited to 8 MiB. Registered
dataset-input grants continue to address only the registered source bytes;
evaluation evidence is read through the authenticated admin evidence route.
Scores, identities and provenance remain in PostgreSQL after evidence expiry.

Leave evaluation admission disabled until the migration, matching Studio node and
agent images, and release acceptance gates pass. Disabling admission does not stop
reconciliation, cancellation, orphan cleanup, evidence expiry or pin recovery.
Recipes with declarations retain durable evaluation obligations while admission
is disabled; expired obligations record a failure rather than disappearing.

Core stores uploaded dataset bytes and metadata. All tokenization/model/Metal work and all
checkpoint/adapter tensor storage remain on Studios; a ready artifact has verified Studio copies.
This setting does not authorize dataset fetching, executable model code, adapter path inputs,
or promotion of unverified adapters into write-capable runs.

### Private training data and release packaging

`coire-training-data` is a named core volume at `/opt/coire/training/datasets` in
**only coire-api and coire-scheduler**, both UID/GID 65532 with read-only rootfs.
Both images seed the empty mountpoint as 0700. API writes bounded UUID-keyed
uploads; scheduler needs write access for staging/orphan/retention cleanup, not
tokenization. No MCP/web/ops/file-worker/model process mounts this volume. Include
it in a consistent private metadata+dataset backup; `docker compose down -v`
would destroy it. Never place weights, checkpoints or serving adapters here.

The shared `x-training-environment` maps the feature flag, fixed volume path,
input API URL, upload/quota/staging/analysis/queue/execution limits into both
services. Its listed `TRAINING_*` overrides are Compose inputs with identical
names. Other runtime variables in the table retain typed defaults unless a
reviewed deployment override explicitly supplies them; merely exporting an
unreferenced host variable does not inject it into a container.

API/scheduler images copy versioned `recipes/training/` and `recipes/images/`
assets to `/app/.venv/recipes/`, the root resolved by the existing non-editable
training loader. Production does not depend on the Git checkout or a recipe
bind mount. Prometheus packages `training.yaml` as `rules/training.yml` to match
the configured glob; diagnostics Grafana packages `jobs.json`.

On core, `scripts/build-node-wheel.sh --local-only` builds core/node wheels,
exports the locked graph and hashes all 87 compatible macOS arm64 dependency
wheels (the count is evidence for the current lock, not an invariant). It does
not contact a Studio or import MLX. Stage/install only through the documented
node deployment workflow. The installer requires `--require-hashes --no-index`,
checks dependencies, smokes offline entry points and exact training API/version
hooks **on the Studio**, then flips a versioned environment symlink. A failure
keeps the previous symlink. No smoke loads model/tokenizer assets; numerical
and actual supervised restart/rollback acceptance are separate gates.

Scheduler wiring needed: start `coire_scheduler.training_metrics.poll_training_metrics(stop)`
after telemetry/database initialization, even with training disabled, and await
it when the lifespan stop event is set. It defaults to a 5-second consistent
read-only Postgres snapshot; failed polls retain the last successful timestamp.
See [baseline wiring](../../docs/runbooks/sft-training.md#baseline-wiring).

Tempo has a fixed 1 GiB container ceiling and a 768 MiB Go runtime memory budget (`GOMEMLIMIT`), leaving headroom for allocations outside the managed heap. Preserve the trace volume when restarting it; use exact trace IDs during workload diagnostics to avoid unnecessary broad searches.

Feature 018 adds `COIRE_PREFERENCE_TRAINING_ENABLED` (default `false`, mapped to
`PREFERENCE_TRAINING_ENABLED`). New preference jobs and measurements also require
`COIRE_TRAINING_ENABLED`; history, cancellation and withdrawal cleanup continue
when admissions are disabled. `COIRE_FEEDBACK_STORAGE_QUOTA_BYTES` maps to
`FEEDBACK_STORAGE_QUOTA_BYTES` (default and maximum 1073741824) and counts copied
feedback content plus private export staging. `COIRE_FEEDBACK_PURGE_BATCH_SIZE`
maps to `FEEDBACK_PURGE_BATCH_SIZE` (default and maximum 100); maintenance erases
withdrawn source copies within 24 hours while preserving published datasets.

See [feedback operations](../../docs/runbooks/feedback.md) for published dataset
retention, source withdrawal and export cleanup, and [preference training](../../docs/runbooks/preference-training.md)
for v3 compatibility, exact profile requirements and drained rollback. Admission
flags do not disable baseline sampling, cancellation or withdrawal maintenance.

### Managed upgrade configuration (019)

Managed admission defaults off (`UPGRADES_ENABLED=false`) until independent recovery bootstrap
and acceptance pass. `UPGRADE_PROJECT` (`coire`) and `UPGRADE_EXECUTOR_ID`
(`coire-upgrade-executor`) bind executor authority. `upgrade_executor_token` is a
Keychain-sourced file secret with a `coire_upgrade_` prefix, mounted only to API, MCP and executor;
it cannot initiate administrator operations. Never place it in a command or environment dump.

`UPGRADE_API_URL` (`http://coire-api:8000`) and `UPGRADE_PROXY_URL`
(`http://docker-socket-proxy:2375`) are internal configured origins. Prepared manifests use
`UPGRADE_PREPARED_DIR` (`/opt/coire/upgrades/prepared`, read-only), including its protected
`backups/` subdirectory for database dumps and isolated restore receipts.
Its `latest.json` is an operator-prepared backup pointer; acceptance retains the exact
receipt and both migration authorization boundaries recheck dump bytes, restored
schema identity, private ownership and the 24-hour limit. It does not authorize a
production restore. Execution receipts use
`UPGRADE_STATE_DIR` (`/opt/coire/upgrades/state`, private); host/executor coordination uses
`UPGRADE_COORDINATION_DIR` (`/opt/coire/upgrades/coordination`). Studio selection uses
`UPGRADE_ENVIRONMENT_ROOT` (`/opt/coire/envs`) and retained recovery interpreters use
`UPGRADE_BOOTSTRAP_ROOT` (`/opt/coire/upgrade-bootstrap`). These files are immutable operation
data/journals, not additional configuration sources.

Native acceptance fixtures set `NODE_REGISTRATION_ENABLED=false` (default `true`)
to run an authenticated node on a private loopback listener without constructing
cluster endpoints or reading the one-time registration secret. This affects only
registration; managed startup verification, maintenance and token checks remain
active. Normal Studio installations retain the default.

With bootstrap ownership committed, `uv run python scripts/coire-deploy.py prepare-backup
/private/operator/backup-catalog --publisher-image sha256:<verified-executor-image-id>`
prepares and publishes the backup through the existing private catalog volume. It takes
the shared deployment lease, refuses active/uncertain ownership or a managed operation
hold, and leaves the selected runtime owner unchanged. The dump and isolated PostgreSQL
17 restore are each bounded to 4 GiB; transfer requires the dump size plus 1 GiB of staging
headroom. Publication uses a disconnected, read-only, capability-dropped container under
UID 65532 and does not mount credentials or the Docker socket. Retried publication verifies
the same immutable bytes; preparation retains private partial artifacts on failure.

`UPGRADE_SMOKE_MODEL_ID` selects an already acquired, registry-verified tiny asset; unset blocks
managed upgrades. `UPGRADE_DRAIN_S` (900, maximum 900), `UPGRADE_READINESS_S` (120, maximum 120),
`UPGRADE_RECOVERY_S` (300, maximum 300), and `UPGRADE_GATEWAY_OUTAGE_S` (300, maximum 300) are
captured at acceptance. `UPGRADE_POLL_S` (2, maximum 5) bounds progress observation;
`UPGRADE_DISK_MARGIN_BYTES` (1 GiB, positive) protects staging headroom. Expired deadlines never
release uncertain maintenance or resource holds. Executor network access was approved on
2026-10-11. The `upgrades` Compose profile starts the independent executor;
`COIRE_UPGRADES_ENABLED` still defaults false until mandatory acceptance passes.
`COIRE_UPGRADE_SMOKE_MODEL_ID` supplies the already verified tiny model UUID to API;
an empty value leaves managed operations blocked. The catalog and journal use private
service-owned `coire-upgrade-prepared` and `coire-upgrade-state` named volumes.
API mounts both read-only; only executor writes the journal. `coire-up` exports
`COIRE_UPGRADE_COORDINATION_DIR` as the project's host deployment coordination path,
and API, scheduler and executor share that exact private bind. Bootstrap must verify
ownership and the namespace UID mapping before qualification; it never widens permissions.

`COIRE_UPGRADE_MCP_URL` defaults to `http://coire-mcp:8001` and selects the private, authenticated MCP request-owner inventory. It does not publish a port. MCP must receive the existing restricted executor credential before managed bootstrap can be ready; an unavailable credential or source inventory keeps upgrades closed.

Prepared Core catalogs must be owned by service UID `65532` with private directories
and `0600` artifact files. A private host catalog owned by the operator is suitable
for local verification, but must not be mounted as the services' readable catalog.
For the service-owned catalog, trusted host preparation uses
`uv run scripts/coire-deploy.py prepare-upgrade <private-host-path> --release-document <private-release.json> --artifact compose.json=<frozen-compose.json> --publisher-image sha256:<verified-executor-image-id>`.
The publisher image must already be present, verified, Linux ARM64, and configured
for UID `65532:65532`. This fixed helper transfers bounded artifact bytes on stdin
into the existing `<project>_coire-upgrade-prepared` volume. It runs without networking,
host bind mounts, secrets, additional capabilities or a writable root filesystem.
It does not acquire images or create catalog volumes. The trusted bootstrap path
must initialize that named volume first. The resulting registration request still
goes through the authenticated, audited admin release API; publication alone does
not authorize an upgrade or establish recovery readiness.

Core release manifests include `host_release_key`, the exact existing directory key
under the project's owned `releases/` catalog. Trusted preparation checks that this
release's frozen Compose bytes match `compose_sha256`; missing keys, symbolic links
and changed bytes are refused. The published artifact catalog and host release key
have separate identities, and both remain retained for runtime selection and rollback.

`UPGRADE_MIGRATION_JOB_ID` and `UPGRADE_MIGRATION_GENERATION` are an optional paired
operation identity for the fixed one-shot migration image. Only the managed executor
supplies them from its accepted plan. Without either value the image performs ordinary
installation migrations. Managed execution requires enabled upgrades, the retained
consumed migration grant, exact source schema, current actor/fences and restore-validated
backup; uncertain attempts remain recorded and cannot rerun automatically.

### Local browser sign-in

`COIRE_LOCAL_AUTH_ENABLED` defaults to `false`; `COIRE_LOCAL_AUTH_ORIGIN` defaults empty.
Local sessions require an exact HTTPS browser origin (loopback HTTP is development-only),
an existing active user with an operator-provisioned password, and migration 0035.
The optional `compose.local-tls.yaml` overlay preserves node HTTP traffic and adds local
HTTPS only after operator approval for the additional listener. TLS key material is
Keychain-backed. See [local sign-in operations](../../docs/runbooks/local-signin.md) for
certificate trust, password provisioning/reset, revocation, observability and rollback.

Qwen photo edits (`mode=edit`) use the bounded `COIRE_IMAGE_EDIT_JOB_TIMEOUT_S` deadline
(default two hours), including queue time. The existing thirty-minute deadline remains
for other image modes. Cancellation and authorization checks remain active throughout.
