# Implementation Plan: Model Registry and Node Agent

**Branch**: `001-model-registry-node-agent` (git branch `feat/001-model-registry-node-agent` per CONTRIBUTING §2) | **Date**: 2026-08-30 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-model-registry-node-agent/spec.md`

## Summary

Make a model a first-class registry object and give the node agent the power to run one. On
core: a `models` table with a five-state lifecycle, admin-only routes guarded by an interim
admin token (ADR-0004), an audit log from the first row, and a reconciler that drives a
pull-once-verify-replicate-verify job whose long-running steps run on the Studios. On each
Studio: the agent gains `inspect`/`pull`/`import`/`verify` jobs executed in a niced worker
subprocess, a mesh-only export path authorised by per-replication transfer grants so the peer
copy never crosses Wi-Fi, and `load`/`unload`/`status`/`reconcile` verbs over bare
`mlx_lm.server` processes that survive an agent restart because they are spawned in their own
session and re-adopted by `(pid, create_time)`. "Ready" means the engine generated a token;
"resident" means the kernel's physical footprint. Feature 002 (conversion, DBOS), 003 (routing),
004 (ledger/eviction) and 005 (instances, drain) are deliberately not pre-empted.

## Technical Context

**Language/Version**: Python 3.13 (pinned; 000 R5). No SPA changes — the console is feature
008; the admin surface here is the API plus `scripts/coire`.

**Primary Dependencies**: as feature 000, plus on the node **mlx-lm 0.31.3 / mlx 0.32.2**
(cp313 arm64 wheels; research R1) and **huggingface_hub 1.29.0** with `hf_xet` (R5); on core
nothing new — the control plane never touches Hugging Face or MLX. `psutil 7.2.2` plus a
`ctypes` shim over `libproc.proc_pid_rusage` for physical footprint (R6). DBOS is **not**
introduced (R7).

**Storage**: Postgres 17 — migration `0002_registry` adds `models`, `model_state_transitions`,
`model_copies`, `download_jobs`, `engine_processes`, `audit_log`. On each Studio, plain files
under `/opt/coire/models/<slug>/` and cache state under `/opt/coire/state/` (data-model.md).

**Testing**: pytest + httpx as in 000; contract tests validate against
`contracts/registry-api.yaml` and `contracts/node-api.yaml`; a fake engine and a fake Hub for
unit tests; the Linux compose integration job gains two node-agent containers on a simulated
mesh; a new `macos-15` job runs the real engine with a 280 MB model (R9). Cable/reboot cases are
manual on the cluster (quickstart §3, §6).

**Target Platform**: `linux/arm64` containers on core (unchanged); native macOS 26.6 on the
Studios; CI on `ubuntu-24.04-arm` and `macos-15`.

**Project Type**: `uv` workspace monorepo — extends `coire-core`, `coire-api`, `coire-node`;
adds a test-only node image. No new package.

**Performance Goals**: one external pull per acquisition (SC-004); replication over the mesh at
≥ 0.5 GB/s expected, measured and recorded (R3); engine `ready` reported within one
health-probe interval of the first generated token; external kill reflected within
`node_engine_health_interval_s` = 5 s (SC-009); `memory_free_bytes` back within 2 % after
unload (SC-006); admin routes p95 ≤ 50 ms excluding node round-trips; node agent budget
unchanged (≤ 2 % core, ≤ 150 MB) because job work runs in a worker process (R5).

**Constraints**: Hugging Face credential exists on the node only (FR-005) — CI's Linux job and
local dev may export `HF_TOKEN` from the gitignored root `.env` for the ungated test model, but
nothing in `deploy/`, any image, or any core container carries it. Engines bind the mesh
address only (FR-018) — reachable from any mesh host, which the spec now states truthfully; per-host firewalling of the engine port is feature 005. Export routes exist only on the mesh listener and the import client has
no egress fallback (FR-007). No caller string ever reaches an engine (FR-017) — the engine is
also started with `HF_HUB_OFFLINE=1` (R1). Studios receive only `mlx-lm` and two directories
beyond 000's footprint (R10). Every admin mutation and refusal writes an audit row (FR-003,
SC-002). Nothing on a Studio is a source of truth (Principle II; R4, R7).

**Scale/Scope**: 2 Studios, 1 core. Roster bounded by the smaller Studio's free disk ÷ 1 copy
(≈ 1.7 TB today) and by 230 GB per-node memory budget for single-node loads. Tens of models,
a handful of engines. First production entry: Qwen3.8-27B (R12, user decision 2026-08-30).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.* — **Evaluated twice;
both passes below.**

| Principle / constraint | Verdict | Evidence |
|---|---|---|
| **I. Bare engines, owned lifecycle** | PASS | `mlx_lm.server` driven directly with a store path (R1); no wrapper. Lifecycle — spawn, ready-probe, footprint, unload, adoption, orphan reporting — is the agent's and is observable and reversible from `/api/v1/admin/engines` (FR-011, FR-015). Engines bind the mesh address only and are reachable from the agent and (from 003) the gateway (FR-018). |
| **II. Control node disposable, workers sacred** | PASS | Truth lives in Postgres on core; Studio files under `/opt/coire/state` are caches the registry reconciles against (R4, R7). No database, no web tier, no second service on a Studio: job work is a child process of the agent (R5). A Studio reboot resumes its jobs and re-adopts its engines without manual steps. |
| **II-a. One service, one container, bare image** | PASS | No new control-plane service; the reconciler is a task inside `coire-api` like 000's prober. The test-only node image is built in CI and never deployed (R9); it is excluded from `images.lock` and from image-policy rule scope by name, and `test_topology` asserts it is absent from the compose project. |
| **III. Contracts first, typed end to end** | PASS | Every shape is a Pydantic model in `coire-core` (`registry.py`, `jobs.py`, `engine.py`, `audit.py`); OpenAPI generated; `contracts/registry-api.yaml` and `contracts/node-api.yaml` are the reviewed shapes CI matches. `NodeStatus` changes are additive with the compatibility note in 000's contract (R11). `/v1` untouched. |
| **IV. Public by design, zero implicit trust** | **EXCEPTION — ADR-0004** (extends ADR-0001) | An interim static admin bearer gates every admin route with 403 + audit; anonymous callers see only published, ready, unentitled-free models (R8). Node routes keep the 000 static token; transfer grants are single-purpose and expiring (R3). Platform still LAN/mesh-only. Time-boxed to 007 (and 005 for node tokens). |
| **V. Models are data, capability is measured** | PASS | Only `POST /api/v1/admin/models` acquires (FR-003/004); the node's `inspect`/`pull` verbs require the node token and are called only by the reconciler. `ready` ⇔ two verified copies (FR-008, R7). Users list only `published` + `ready` + entitled (`/api/v1/models`). Only registry ids are accepted anywhere; engines receive store paths (FR-017). Capability profile stored with `verified: false` and not editable here; 017 sets it. `chat_template` override stored on the record and passed to the engine as `--chat-template` (Principle V's roster-record list, complete). |
| **VI. Observable or it doesn't ship** | **EXCEPTION — ADR-0003 extended** | Metrics `coire_model_state`, `coire_download_bytes_total`, `coire_engine_state`, `coire_engine_resident_bytes`, `coire_engine_load_seconds` and spans for every reconciler stage and node verb export via OTLP (R13). Panels and alerts remain feature 009, whose acceptance list gains the download-stalled and engine-failed alerts and the models panel; ADR-0003 is amended to say so. |
| **VII. Spec-driven, test-gated, incremental** | PASS | Contract tests for both APIs; integration against a ≤ 1 GB model (280 MB) on the compose job; the real-engine layer on a macOS runner (R9). Smallest change: no DBOS, no ledger, no drain, no variants — each named for its feature (R13). |
| Tech: Python 3.13 / uv / FastAPI / Pydantic v2 / SQLAlchemy 2 / Alembic / Postgres 17 | PASS | Unchanged. |
| Tech: DBOS for durable workflows | **EXCEPTION — ADR-0005** (R7) | One linear job with node-side persistence; DBOS arrives in 002 wrapping these node verbs unchanged. Time-boxed to feature 002. |
| Tech: node runtime — MLX/mlx-lm pinned in a lockfile, versioned envs, symlink flip | PASS | `mlx-lm==0.31.3`, `mlx==0.32.2`, `huggingface_hub==1.29.0` pinned in `uv.lock` for `coire-node`; installed into `/opt/coire/envs/0.2.0` by the existing `install.sh` (R10). |
| Tech: hosts by DNS names, never raw IPs | PASS (ADR-0002) | Nodes address each other as `<name>.mesh`; the engine binds the address the agent resolved from the managed hosts block. No IP in any config. |
| Tech: secrets in Keychain, file-mounted | PASS | `coire-admin-token` (core) and `coire-hf-token` (Studio System keychain) follow 000's exact plumbing; `settings.py` reads `/run/secrets/admin_token`; the node reads its Keychain item at start. |
| Tech: forbidden — hand-edited prod config on nodes | PASS | `install.sh` renders the plist (now with `AbandonProcessGroup`); the store and state directories are created by it. |
| Tech: forbidden — long-lived static tokens | Covered by ADR-0001/0004 | Admin token and node token are both time-boxed; transfer grants are not long-lived. |
| Quality: ledger vs RSS drift alert > 10 % | PASS (data) | `resident_delta_bytes` recorded per load (FR-014, R6); the alert itself is 004/009. |
| Quality: scheduler must not admit a load into swap | PASS (flat budget) | FR-020 refuses when committed + estimate > budget (R6); eviction is 004. |
| Quality: node-agent restart re-adopts processes | PASS | R4; SC-007 in quickstart §6 and the macOS CI layer. |
| Quality: documentation → `docs/runbooks/` | PASS (task) | `docs/runbooks/models.md`: add, watch, retry, retire; load/unload; what to do with an orphan; where the store and state live; how to rotate the HF token. |

**Post-design re-check (after Phase 1)**: unchanged. The data model keeps every source of
truth on core; the node contract's only unauthenticated routes are the grant-scoped export
routes, which are mesh-only and expire; the registry contract's user-facing route carries no
paths or copies. Both exceptions remain exactly as scoped in ADR-0003 and ADR-0004.

## Project Structure

### Documentation (this feature)

```text
specs/001-model-registry-node-agent/
├── plan.md              # This file
├── research.md          # Phase 0 — R1–R13
├── data-model.md        # Phase 1 — Model, CapabilityProfile, ModelCopy, ChecksumManifest,
│                        #   DownloadJob, EngineProcess, AuditRecord, node-side state, settings
├── quickstart.md        # Phase 1 — validation scenarios mapped to SC-001…SC-009
├── contracts/
│   ├── registry-api.yaml       # OpenAPI 3.1: /api/v1/admin/models…, /api/v1/models, engines, nodes, audit
│   └── node-api.yaml           # OpenAPI 3.1: /node/models…, /node/jobs…, /node/export…, /node/engines…
└── tasks.md             # Phase 2 — /speckit-tasks (not created here)

docs/adr/0004-interim-admin-token-until-roles.md      # new
docs/adr/0005-defer-dbos-to-acquisition-pipeline.md    # new
docs/adr/0003-…                                       # amended: 009's acceptance list grows
specs/000-bootstrap/contracts/health-api.yaml         # amended: NodeStatus compatibility note (R11)
```

### Source Code (repository root)

```text
packages/coire-core/src/coire_core/
├── models/registry.py     # ModelState, Visibility, Tag, PlacementPolicy, CapabilityProfile,
│                          #   Model, ModelDetail, ModelListing, ModelCopy, ModelAddRequest,
│                          #   ModelUpdateRequest, ModelRejected, LoadRefused
├── models/jobs.py         # DownloadJob, DownloadStage, JobStatus, JobKind, RepoInspection,
│                          #   ChecksumManifest (+ canonical_bytes(), sha256())
├── models/engine.py       # EngineState, EngineProcess, EngineStatus, ReconcileRequest/Result,
│                          #   BudgetRefused
├── models/audit.py        # AuditRecord, AuditOutcome
├── models/node.py         # NodeStatus += engines, jobs, memory_budget_bytes, memory_committed_bytes,
│                          #   store_free_bytes (additive; R11)
├── memory.py              # estimate_bytes(inspection, settings), fits(...) — pure functions (R6)
├── net.py                 # MeshClient(fallback=False) option for replication (R3)
└── settings.py            # admin_token, hf_token, node_store_dir, node_state_dir, engine range,
                           #   budget fraction, health/start timeouts, overhead table, disk reserve

apps/coire-api/src/coire_api/
├── auth.py                # PrincipalKind.ADMIN; require_principal reads /run/secrets/admin_token;
│                          #   require_admin → 403 + audit (ADR-0004)
├── audit.py               # write_audit(session, actor, action, target, outcome, detail)
├── db.py                  # ModelRow, ModelStateTransitionRow, ModelCopyRow, DownloadJobRow,
│                          #   EngineProcessRow, AuditRow
├── alembic/versions/0002_registry.py
├── registry/
│   ├── service.py         # add_model (inspect → fit → row), update, retire, delete, retry,
│   │                      #   transition() — the state machine in one place
│   ├── placement.py       # choose_origin(nodes) (most free store), choose_load_node(policy)
│   └── reconciler.py      # DownloadJob cursor driver + engine reconciler (R7); lifespan task
├── nodes_client.py        # typed client over contracts/node-api.yaml using MeshClient + node token
├── routes/admin_models.py # /api/v1/admin/models…
├── routes/admin_engines.py# /api/v1/admin/engines…
├── routes/admin_nodes.py  # /api/v1/admin/nodes, /api/v1/admin/audit
├── routes/models.py       # /api/v1/models (user-facing, filtered)
└── routes/nodes.py        # register → schedule reconcile for that node
apps/coire-api/tests/
├── contract/test_registry_api.py   # shapes vs registry-api.yaml; admin guard over every admin path
├── unit/test_state_machine.py, test_memory.py, test_placement.py, test_reconciler.py

apps/coire-node/src/coire_node/
├── agent.py               # routers mounted; export routes on the mesh app only
├── routes/models.py       # /node/models, inspect, delete, export grant
├── routes/jobs.py         # /node/jobs/pull|import|verify, get, cancel
├── routes/export.py       # /node/export/{grant}/… (Range; mesh listener only)
├── routes/engines.py      # /node/engines…, reconcile
├── store.py               # slug ↔ path, manifest read/write, canonical hashing, disk free
├── hub.py                 # huggingface_hub wrappers: inspect(), snapshot(), errors → kinds (R2)
├── jobs.py                # JobSupervisor: state files, worker spawn (nice 10), progress, resume (R5)
├── worker.py              # `python -m coire_node.worker <state>`: pull / import / verify bodies
├── engines.py             # EngineManager: spawn (setsid), ready-probe, health loop, footprint,
│                          #   engines.json, adopt/reconcile, stop (R1, R4, R6)
├── footprint.py           # proc_pid_rusage ctypes shim; rss fallback (R6)
├── grants.py              # transfer-grant registry with expiry (R3)
├── metrics.py             # += engines[], jobs[], budget/committed, store_free_bytes
└── testing/
    ├── fake_engine.py     # /health, /v1/models, one-token /v1/chat/completions; --delay, --fail-on-start
    └── fake_hub.py        # local Hub API + blobs for a synthetic repo
apps/coire-node/
├── docker/node-test.Dockerfile   # CI only: Linux image of the agent + fake engine (R9)
├── install.sh                    # 0.2.0; creates models/, state/, hf-cache/; plist gets AbandonProcessGroup
└── tests/{unit,contract,engine}/ # engine/ marked -m engine (macOS only)

deploy/
├── compose/compose.yaml          # coire-api: + admin_token secret
├── compose/compose.override.it.yaml   # integration: 2 node-test containers, coire-mesh-sim 192.168.100.0/24
├── compose/coire-up              # + coire-admin-token → admin_token
└── launchd/com.coire.node.plist.template   # + AbandonProcessGroup true, HF_HOME, store/state env

scripts/
├── coire                        # curl+jq admin helper (quickstart)
└── coire-secrets-init.sh        # + coire-admin-token; --show-hf-token-command

tests/integration/
├── test_acquisition.py          # SC-001, SC-003 (dup, not-MLX, no-fit), SC-004 (origin count)
├── test_admin_guard.py          # SC-002 + audit rows
├── test_engines_fake.py         # load/unload/adopt/orphan with the fake engine
└── test_topology.py             # + node-test image absent from the prod project

.github/workflows/ci.yml         # integration: override file; new `engine` job on macos-15
docs/runbooks/models.md
```

**Structure Decision**: Extend the three existing packages rather than add one. The registry
lives in `coire-api` under a `registry/` package so the state machine, placement and reconciler
are separable from routes and testable without HTTP; the node agent gains one module per
concern (store, hub, jobs, worker, engines, grants) so feature 002 can add `convert`/`validate`
as new job kinds in `worker.py` and feature 005 can replace `engines.py`'s state with
`ModelInstance` without touching the routes. The test-only node image is the one addition that
looks like a fourth deployable but is not: it exists so the Linux integration job can run two
agents on a real-looking mesh, and the topology test proves it never enters the compose project.

## Complexity Tracking

> Filled because the Constitution Check records one exception and one deviation.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| **Principle IV** — interim static admin bearer (ADR-0004, extends ADR-0001) | The registry's first routes must refuse non-admins with 403 and an audit row (US2, SC-002), and 000 shipped no credential of any kind. A shared secret through the existing `require_principal` seam is the smallest real gate; 007 replaces the body. | "Everyone is admin until 007" — SC-002 untestable; an open write surface the day the tunnel opens. Pulling 007 forward — blocks the registry on an identity provider that isn't configured. |
| **Tech constraint: DBOS for durable workflows** — reconciler over a stage cursor instead (ADR-0005, R7) | One linear job whose long steps run and persist on nodes; the control plane's part is idempotent re-issue of the current stage. DBOS is adopted in 002 wrapping the same node verbs, which do not change. | DBOS now — its schema and runtime in `coire-api` for a workflow with no branching, and it pre-empts 002's design. Celery/RQ — a broker on core for one job type. |
| **Principle VI** — no panel or alert here (ADR-0003 extended) | Backends are 009; export is wired and metric names fixed so 009 adds panels without touching this code. | Grafana/Prometheus in 001 — duplicates 009. |
