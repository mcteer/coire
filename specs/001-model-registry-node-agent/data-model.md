# Data Model: Model Registry and Node Agent

**Feature**: `001-model-registry-node-agent` · **Date**: 2026-08-30

All wire shapes are Pydantic v2 models in `packages/coire-core/src/coire_core/models/`
(`registry.py`, `jobs.py`, `engine.py`, `audit.py`) with `extra="forbid"`. Persisted entities
live in Postgres on core via Alembic migration `0002_registry`. The Studios hold **caches, never
truth** (Principle II): the node-local state files below exist only so an agent can find its own
processes and resume its own jobs after a restart; the registry reconciles against them.

Feature 000's `Node` is unchanged. Feature 001 attaches `ModelCopy`, `DownloadJob` and
`EngineProcess` to it, exactly as 000's data model reserved.

## Model *(persisted: `models`)*

The registry record. One row per Hugging Face repo id; variants and conversions are feature 002
and will hang off this row rather than duplicate it.

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK | the registry id; the only identifier a caller may ever present (FR-017) |
| `repo_id` | str | unique, `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$` | Hugging Face `org/name` |
| `slug` | str | unique, derived | `repo_id` with `/` → `--`; the store directory name on every node |
| `display_name` | str | non-empty | defaults to the repo name |
| `description` | str \| null | | the picker's one-line "good for" |
| `state` | enum `downloading` \| `replicating` \| `ready` \| `failed` \| `retired` | required | FR-002 |
| `state_reason` | str \| null | | why it is `failed`, or why `replicating` is waiting |
| `visibility` | enum `admin_only` \| `published` | default `admin_only` | |
| `entitlement` | list[str] | JSONB, default `[]` | roles/users allowlist; empty = every entitled caller. Feature 007 gives it real subjects |
| `tags` | list[str] | JSONB, each in `coding` `general` `reasoning` `vision` `image` | ARCHITECTURE 3.2 vocabulary |
| `placement_policy` | str | `single:auto` \| `single:<node>` \| `pinned:<node>` \| `sharded:tp` \| `sharded:pp` | stored and validated; only `single:*` and `pinned:*` are exercised here (sharded is 006) |
| `precision` | str | e.g. `4bit-g64`, `8bit`, `bf16` | from `config.json` `quantization` (bits, group_size) or dtype |
| `weight_bytes` | int | > 0 | sum of weight-file sizes from inspection |
| `total_bytes` | int | > 0 | every file in the repo snapshot |
| `file_count` | int | > 0 | |
| `memory_estimate_bytes` | int | > 0 | `weight_bytes × overhead(precision)` + KV headroom (research R6), computed at add time (FR-010) |
| `idle_ttl_seconds` | int \| null | ≥ 60 or null | null = never. Stored only; feature 004 acts on it |
| `capability_profile` | CapabilityProfile | JSONB | see below |
| `context_window` | int \| null | | from `max_position_embeddings`; duplicated into the profile for the picker |
| `manifest_sha256` | str \| null | hex | digest of the checksum manifest document once the pull verifies |
| `created_at` / `updated_at` / `ready_at` | datetime | | `ready_at` set once, on first `ready` |

**Validation**: `repo_id` must inspect as MLX-format (research R2); a non-MLX repo is rejected
with 422 and a message naming feature 002 (clarification 1). The memory estimate must fit at
least one supported placement on the declared inventory, and `total_bytes` must fit under the
free disk of *both* Studios less a reserve, before any bytes move (FR-010, edge case 1).

**State transitions** (every one appends a `ModelStateTransition`, FR-002):

```
                 add (admin)                    both copies verified
   ∅ ──────────────────────▶ downloading ──▶ replicating ──────────────▶ ready
                                  │                │                       │
       pull/verify failed         │                │ peer verify failed    │ retire (admin)
       inspect rejected at add ───┴──▶ failed ◀────┘                       ▼
                                          │  retry (admin) → downloading   retired
                                          └────────────────────────────▶ (DELETE: row removed,
                                                                           allowed only from failed)
```

`replicating` is entered when the first copy verifies and left only when the second verifies or
fails. A peer that is unreachable keeps the model in `replicating` with `state_reason` set
(edge case 4); it never becomes `ready` on one copy (FR-008, SC-003). `retired` is terminal:
engines are unloaded, both copies deleted, the row kept for audit (US5 scenario 3).

**Relationships**: 1→N `ModelCopy` (one per node), 1→N `DownloadJob`, 1→N `EngineProcess`,
1→N `ModelStateTransition`.

## CapabilityProfile *(embedded JSONB on `models`)*

| Field | Type | Default | Notes |
|---|---|---|---|
| `tool_calling` | enum `none` \| `prompted` \| `native` | `none` | harness behaviour is selected from this, never from the model name (Principle V) |
| `structured_output` | enum `none` \| `json_mode` \| `json_schema` | `none` | |
| `context_window` | int \| null | from config | tokens |
| `reasoning` | enum `none` \| `thinking` \| `hybrid` | `none` | |
| `parallel_tools` | bool | false | |
| `chat_template_present` | bool | from inspection | whether `tokenizer_config.json` carries a template |
| `verified` | bool | false | set only by feature 017's harness evaluation; the router refuses unverified models for `apply` |

Admins edit every field except `verified` (US5).

## ModelStateTransition *(persisted: `model_state_transitions`)*

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `model_id` | UUID FK | |
| `from_state` | enum \| null | null for creation |
| `to_state` | enum | |
| `reason` | str | never empty |
| `at` | datetime | |

Append-only.

## ModelCopy *(persisted: `model_copies`)*

A model's presence on one node. Unique on (`model_id`, `node_id`).

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `model_id` | UUID FK | |
| `node_id` | UUID FK → `nodes` | |
| `path` | str | absolute path on that node: `/opt/coire/models/<slug>` |
| `bytes` | int | on-disk size reported by the node |
| `manifest_sha256` | str \| null | must equal `Model.manifest_sha256` when verified |
| `verified` | bool | false until the node's file-by-file check passes (FR-008) |
| `verified_at` | datetime \| null | |
| `mismatched_paths` | list[str] | JSONB; populated on failure and the copy deleted (FR-009) |
| `role` | enum `origin` \| `replica` | which copy came from Hugging Face; SC-004 asserts exactly one `origin` per model |

`Model.state == ready ⇔ count(copies where verified) == 2` is an invariant the reconciler
asserts on every pass, not a value it trusts from a previous pass.

## ChecksumManifest *(document; stored on the node beside the copy, digest in the registry)*

Produced at pull time by the origin node; carried to the replica in the import request; recomputed
there file by file (clarification 2).

| Field | Type | Notes |
|---|---|---|
| `slug` | str | |
| `repo_id` | str | |
| `revision` | str | the Hugging Face commit sha the snapshot was taken at |
| `files` | list[{`path`, `bytes`, `sha256`, `upstream_sha256` \| null}] | sorted by path; `upstream_sha256` is Hugging Face's LFS digest when it publishes one, and the pull is refused if the two disagree |
| `total_bytes` | int | |
| `created_at` | datetime | |

Serialised canonically (sorted keys, no whitespace) so its SHA-256 is stable across nodes.

## DownloadJob *(persisted: `download_jobs`)*

The control plane's record of one acquisition. The long-running work happens on nodes as
node-local jobs; this row is the reconciler's cursor (research R7).

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | PK; also the `job_id` presented to nodes so every node verb is idempotent on it |
| `model_id` | UUID FK | |
| `origin_node_id` | UUID FK | the Studio with the most free disk at add time (FR-006) |
| `replica_node_id` | UUID FK | the other Studio |
| `stage` | enum `inspect` \| `pull` \| `verify_origin` \| `export` \| `import` \| `verify_replica` \| `done` \| `failed` | in order; the reconciler only ever advances or fails |
| `bytes_done` / `bytes_total` | int | mirrored from the active node job for the admin's progress view (US1 scenario 2) |
| `files_done` / `files_total` | int | |
| `transfer_grant` | str \| null | random 32-byte token, single model, single use, 24 h TTL; created at `export`, revoked at `done`/`failed` (research R3) |
| `failure_reason` | str \| null | includes `gated` verbatim when the repo is gated (edge case 5) |
| `attempt` | int | incremented by admin retry |
| `started_at` / `updated_at` / `finished_at` | datetime | |

**Stage transitions**: strictly `inspect → pull → verify_origin → export → import → verify_replica → done`;
any stage → `failed`. A control-plane restart re-reads `stage` and re-issues the current node
verb; every node verb is idempotent on `job_id`, so re-issuing is safe (edge cases 2 and 3).

## EngineProcess *(persisted: `engine_processes`)*

A running (or recently running) engine owned by a node agent. Feature 005 generalises this into
`ModelInstance`; the fields here are the subset that instance needs and are named to survive it.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | PK; the `engine_id` presented to the node |
| `model_id` | UUID FK | |
| `node_id` | UUID FK | |
| `port` | int | allocated by the node from its engine range; bound on the mesh address only (FR-018) |
| `pid` | int \| null | |
| `process_create_time` | float \| null | seconds since epoch as the kernel reports it; `(pid, create_time)` is the process identity used for adoption (clarification 3, research R4) |
| `state` | enum `starting` \| `ready` \| `stopping` \| `stopped` \| `failed` \| `orphan` | `ready` only after the engine answered its own health probe (FR-012) |
| `state_reason` | str \| null | exit status and captured output on `failed` (US3 scenario 4) |
| `estimate_bytes` | int | copied from the model at load time |
| `resident_bytes` | int \| null | measured by the node once `ready` (FR-014, research R6) |
| `resident_delta_bytes` | int \| null | `resident − estimate`; feature 004's drift alert reads this |
| `last_health_at` | datetime \| null | |
| `started_at` / `stopped_at` | datetime | |

**State transitions**: `starting → ready` (health probe answered) · `starting → failed` (exited
during start) · `ready → stopping → stopped` (unload) · `ready → failed` (died; detected within one
health interval, FR-016) · `∅ → orphan` (process found on the node with no matching row after
restart, FR-015). `orphan` rows are created by reconciliation and only an admin unload removes
them.

## AuditRecord *(persisted: `audit_log`)*

Append-only; no route deletes or modifies it (feature 007 FR-018 is honoured from the first row).

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `at` | datetime | |
| `actor` | str | `admin-token` until feature 007 provides subjects (ADR-0004) |
| `action` | str | `model.add` `model.retry` `model.update` `model.publish` `model.unpublish` `model.retire` `model.delete` `engine.load` `engine.unload` |
| `target_type` / `target_id` | str / str | |
| `outcome` | enum `ok` \| `refused` \| `error` | refused writes a row too: SC-002 is checked against the audit log |
| `detail` | JSONB | never contains a presented secret |

## Node-side state *(files under `/opt/coire/state/`, not truth)*

| File | Content | Purpose |
|---|---|---|
| `engines.json` | list of `{engine_id, slug, port, pid, create_time, started_at}` | adoption after agent restart (FR-015). Rewritten atomically on every spawn/stop |
| `jobs/<job_id>.json` | `{job_id, kind, stage, params, bytes_done, error}` | resume after agent restart or node reboot (edge case 3). Removed on `done` after the control plane has read it |
| `models/<slug>.manifest.json` | ChecksumManifest | verification and export |
| `store/` | the models themselves: `/opt/coire/models/<slug>/…` | plain files (no cache symlinks) so an engine can be pointed at the directory |

## Node status extension *(wire; `NodeStatus` gains additive fields)*

| Field | Type | Notes |
|---|---|---|
| `engines` | list[EngineStatus] | per-process CPU % and resident bytes for every engine the agent owns, including orphans (FR-013) |
| `jobs` | list[JobSummary] | active node jobs with stage and bytes |
| `memory_budget_bytes` | int | `memory_total × node_memory_budget_fraction` (research R6) |
| `memory_committed_bytes` | int | Σ `estimate_bytes` of engines in `starting`/`ready`; FR-020 refuses when a load would push this past the budget |
| `store_free_bytes` | int | free space on the volume holding `/opt/coire/models` |

Additive only; feature 000's `contracts/health-api.yaml` is amended with a compatibility note
(Principle III) and its contract test relaxed on `NodeStatus` to `additionalProperties` for
these named fields.

## Settings additions *(`coire_core.settings.Settings`)*

| Field | Source | Default | Notes |
|---|---|---|---|
| `admin_token` | `/run/secrets/admin_token` (core) | `""` | interim admin bearer (ADR-0004) |
| `hf_token` | System keychain item `coire-hf-token` (node only) | `""` | never present on core (FR-005) |
| `node_store_dir` | env | `/opt/coire/models` | |
| `node_state_dir` | env | `/opt/coire/state` | |
| `node_engine_port_range` | env | `9500-9599` | |
| `node_memory_budget_fraction` | env | `0.90` | 230 GB of 256 GB, as ARCHITECTURE §4 assumes |
| `node_engine_health_interval_s` | env | `5.0` | FR-016's detection bound |
| `node_engine_start_timeout_s` | env | `600` | large models take minutes from SSD |
| `memory_overhead_by_precision` | env (JSON) | see research R6 | estimate multipliers |
| `disk_reserve_bytes` | env | 50 GiB | kept free on every Studio when checking fit |
| `registry_reconcile_interval_s` | env | `5.0` | the download-job and engine reconcilers |
