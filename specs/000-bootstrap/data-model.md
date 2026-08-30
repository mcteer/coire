# Data Model: Bootstrap Control Plane Skeleton

**Feature**: `000-bootstrap` · **Date**: 2026-08-29

All wire shapes are Pydantic v2 models in `packages/coire-core` with
`model_config = ConfigDict(extra="forbid")`. Only `Node` is persisted (Postgres, via Alembic
migration `0001_nodes`). Everything else is computed on request.

## Node *(persisted)*

A declared Studio, registered by its node agent.

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK | Assigned at registration |
| `name` | str | unique, `^coire-[a-z0-9-]+$` | e.g. `coire-edge-a`; must appear in `deploy/cluster/nodes.yaml` |
| `role` | enum `studio` \| `core` | required | Only `studio` registers in 000 |
| `mesh_address` | IPv4 | required | Presented by the agent at registration; the `.mesh` name resolves to it |
| `egress_address` | IPv4 | required | Wi-Fi address; used only for alerted fallback |
| `memory_total_bytes` | int | > 0 | From the agent |
| `disk_total_bytes` | int | > 0 | From the agent |
| `gpu_cores` | int \| null | ≥ 0 | From the agent (80 / 60 on this cluster) |
| `agent_version` | str | semver | Which `/opt/coire/envs/<version>` is active |
| `registered_at` | datetime | set once | |
| `last_seen_at` | datetime | updated on every health probe | Drives `reachability` |
| `reachability` | enum `healthy` \| `degraded` \| `unreachable` \| `unknown` | derived | `unknown` until first probe; `degraded`/`unreachable` thresholds are feature 009's — 000 sets only `healthy`/`unreachable`/`unknown` |

**Validation**: registration is rejected if `name` is not in the declared inventory, if the
presented per-node token does not match the Keychain-sourced value for that name, or if
`mesh_address` is not within `192.168.100.0/24`.

**State transitions**: `unknown → healthy` on first successful probe; `healthy → unreachable`
after `probe_failures ≥ 3` consecutive (interval 10 s); `unreachable → healthy` after one
success. Damped transitions and `degraded` arrive with feature 009.

**Relationships**: none in 000. Feature 001 attaches `ModelCopy` and `EngineProcess`; feature
005 attaches `Registration` tokens.

## NodeRegistration *(request)*

What the agent sends to `POST /api/v1/nodes/register`.

| Field | Type | Constraints |
|---|---|---|
| `name` | str | as above |
| `token` | SecretStr | static per-node token from the System keychain (replaced in 005) |
| `mesh_address` | IPv4 | |
| `egress_address` | IPv4 | |
| `memory_total_bytes` | int | > 0 |
| `disk_total_bytes` | int | > 0 |
| `gpu_cores` | int \| null | |
| `agent_version` | str | |

Response: the persisted `Node` minus `token`.

## NodeStatus *(node agent `/health` response)*

| Field | Type | Notes |
|---|---|---|
| `name` | str | |
| `agent_version` | str | |
| `uptime_seconds` | float | |
| `cpu_percent` | float 0–100 | whole-node, sampled |
| `gpu_percent` | float 0–100 \| null | IOAccelerator "Device Utilization %"; null if unavailable |
| `thermal_state` | enum `nominal` \| `fair` \| `serious` \| `critical` \| `unknown` | |
| `memory_total_bytes` | int | |
| `memory_free_bytes` | int | |
| `disk_total_bytes` | int | |
| `disk_free_bytes` | int | |
| `agent_cpu_percent` | float | the agent's own use (FR-012c) |
| `agent_rss_bytes` | int | the agent's own use (FR-012c) |
| `collection_budget_ok` | bool | false if the agent exceeded its configured budget in the last window |
| `path` | enum `mesh` \| `fallback` | which listener answered — makes fallback visible per response |
| `sampled_at` | datetime | observation time; consumers treat > 30 s as stale |

## ServiceHealth and HealthResponse *(control-plane `/health`)*

`ServiceHealth`:

| Field | Type | Notes |
|---|---|---|
| `name` | str | `api`, `postgres`, `mcp`, `scheduler`, `otel-collector`, or a node name |
| `healthy` | bool | |
| `detail` | str \| null | reason when unhealthy |
| `checked_at` | datetime | |
| `latency_ms` | float \| null | probe round-trip |

`HealthResponse`:

| Field | Type | Notes |
|---|---|---|
| `status` | enum `healthy` \| `degraded` \| `unhealthy` | `degraded` if any non-critical dependency failed; `unhealthy` if Postgres is down |
| `version` | str | image tag of `coire-api` |
| `services` | list[ServiceHealth] | control-plane services |
| `nodes` | list[ServiceHealth] | one per registered node, `healthy` from `reachability` |
| `generated_at` | datetime | |

HTTP status is 200 for `healthy` and `degraded`, 503 for `unhealthy`, so a load balancer can
key on it while the body says why.

## ReadyResponse *(every long-lived service `/ready`)*

| Field | Type |
|---|---|
| `service` | str |
| `version` | str |
| `ready` | bool (always true if the process can answer) |

Used by compose healthchecks. Deliberately checks nothing external.

## ImageTag *(CI artefact, not persisted by the app)*

Recorded as CI job outputs and as OCI annotations on the pushed image.

| Field | Type | Notes |
|---|---|---|
| `service` | str | `coire-api` … `coire-agent` |
| `git_tag` | str | |
| `architecture` | `linux/arm64` | only value in 000 |
| `digest` | sha256 | what compose pins |
| `sbom_ref` | str | SPDX JSON artefact |
| `scan_verdict` | enum `pass` \| `fail` | Trivy, CRITICAL threshold |
| `policy_verdict` | enum `pass` \| `fail` | shell absent, non-root, RO-compatible |

## Settings *(`coire_core.settings.Settings`)*

pydantic-settings; secrets are read from `/run/secrets/<name>` files, never environment.

| Field | Source | Notes |
|---|---|---|
| `database_url` | assembled from `POSTGRES_HOST` env + `/run/secrets/postgres_password` | |
| `key_signing_secret` | `/run/secrets/key_signing_secret` | declared now, used from 007 |
| `otlp_endpoint` | env, default `http://otel-collector:4317` | |
| `mesh_hosts_file` | env, default `/etc/hosts` | node side |
| `node_token` | System keychain (node) / `/run/secrets/node_tokens` (api) | static; 005 replaces |
| `node_probe_interval_s` | env, default 10 | |
| `node_collection_budget_cpu_pct` | env, default 2.0 | FR-012c |
| `node_collection_budget_rss_bytes` | env, default 150 MiB | FR-012c |
