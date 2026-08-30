---
description: "Task list for feature 001 — Model Registry and Node Agent"
---

# Tasks: Model Registry and Node Agent

**Input**: Design documents from `/specs/001-model-registry-node-agent/`

**Prerequisites**: plan.md, spec.md, research.md (R1–R13), data-model.md, contracts/ (registry-api.yaml, node-api.yaml), quickstart.md. Feature 000 merged on `main`; **000's T063 (node agent installed on both Studios) is a prerequisite for every task marked *(cluster)*** and still needs the operator's `sudo`.

**Tests**: INCLUDED. Constitution Principle VII requires contract tests for every API surface and an integration test against a ≤ 1 GB model; SC-002…SC-009 are checks by definition. Test tasks precede implementation within each story and must fail first. Three test layers per research R9: unit/contract (any OS), `integration` (Linux compose + two node-test containers on a simulated mesh), `engine` (macOS runner, real `mlx_lm.server`).

**Organization**: Grouped by user story. US1 (acquire → `ready`) and US3 (load/unload) are the two independent primitives and can proceed in parallel after Phase 2; US2 (admin-only) hardens the routes US1 creates; US4 (adoption) builds on US3; US5 (curation) builds on US1's rows. Node-side and core-side tasks within a story touch disjoint files and are marked [P] where so.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1–US5 from spec.md
- Every task names exact file paths

## Path Conventions

`uv` workspace per plan.md: `packages/coire-core/src/coire_core/`, `apps/coire-api/src/coire_api/`, `apps/coire-node/src/coire_node/`; tests under each package's `tests/{unit,contract,engine}/`; cross-cutting integration tests under repo-root `tests/integration/`; deploy under `deploy/{compose,launchd}/`; helpers under `scripts/`. Contracts are validated the way 000 does it (`jsonschema` against the YAML, `$ref` rewritten to `$defs`).

**Conventions that apply to every task**: every wire shape is a `coire-core` Pydantic model with `extra="forbid"` (Principle III); every admin route depends on `require_admin` and writes an audit row (FR-003); no code path passes a caller string to an engine — only `store.path_for(slug)` (FR-017); node addresses are `<name>.mesh` via `MeshClient`, never an IP (ADR-0002); nothing token-shaped is logged (`SecretStr` everywhere).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependencies, settings, secrets plumbing, and the Studio footprint additions. Nothing behaves differently yet.

- [ ] T001 Add `mlx-lm==0.31.3`, `mlx==0.32.2; platform_system=="Darwin"`, `huggingface_hub[hf_xet]==1.29.0` to `apps/coire-node/pyproject.toml` dependencies; bump `version` to `0.2.0` and `__version__` in `apps/coire-node/src/coire_node/__init__.py`; run `uv lock` and commit `uv.lock` (research R10)
- [ ] T002 [P] Add settings from data-model.md "Settings additions" to `packages/coire-core/src/coire_core/settings.py`: `admin_token: SecretStr`, `hf_token: SecretStr`, `node_store_dir`, `node_state_dir`, `node_hf_cache_dir`, `node_engine_port_range: str = "9500-9599"` (+ parsed property), `node_memory_budget_fraction`, `node_engine_health_interval_s`, `node_engine_start_timeout_s`, `memory_overhead_by_precision: dict[str, float]` (R6 defaults), `kv_headroom_tokens: int = 32768`, `disk_reserve_bytes`, `registry_reconcile_interval_s`; extend `packages/coire-core/tests/test_settings.py` for defaults and the port-range parser
- [ ] T003 [P] Add `coire-admin-token` to `scripts/coire-secrets-init.sh` (created with `openssl rand -base64 32`; `--show-hf-token-command` prints the `sudo security add-generic-password -a coire -s coire-hf-token … /Library/Keychains/System.keychain` line for each Studio); add the item to `SECRET_ITEMS`/`SECRET_VARS`/`SECRET_FILES` in `deploy/compose/coire-up` (→ `admin_token`); declare the `admin_token` secret in `deploy/compose/compose.yaml` and mount it on `coire-api` only; add `COIRE_SECRET_ADMIN_TOKEN` to `INTEGRATION_SECRETS` in `tests/integration/conftest.py` (ADR-0004)
- [ ] T004 [P] Update `deploy/launchd/com.coire.node.plist.template`: add `<key>AbandonProcessGroup</key><true/>`, `<key>ExitTimeOut</key><integer>30</integer>`, and environment `HF_HOME=__PREFIX__/hf-cache`, `NODE_STORE_DIR=__PREFIX__/models`, `NODE_STATE_DIR=__PREFIX__/state`, `HF_HUB_DISABLE_PROGRESS_BARS=1` (research R4, R5)
- [ ] T005 [P] Update `apps/coire-node/install.sh` (AGENT_VERSION 0.2.0) to create `$PREFIX/models`, `$PREFIX/state/jobs`, `$PREFIX/hf-cache`; `install.sh` also prints the `coire-hf-token` System-keychain command alongside the node-token one (the agent reads it at start, T006); update `apps/coire-node/uninstall.sh --dry-run` to enumerate the three new directories and the second Keychain item; update the `--dry-run` expectations in `docs/runbooks/bootstrap.md` §Node agent
- [ ] T006 [P] Add the node's Keychain read for `coire-hf-token` in `apps/coire-node/src/coire_node/__main__.py` (same `security find-generic-password -w -s coire-hf-token /Library/Keychains/System.keychain` pattern as `coire-node-token`; absent → empty `SecretStr`, WARNING logged once, ungated pulls still work); export it as `HF_TOKEN` only into the worker subprocess environment (T028), never into the agent's own `os.environ`
- [ ] T007 [P] Add `scripts/coire` — bash 3.2, `curl`+`jq` wrapper over the admin routes using `COIRE_ADMIN` (or `security find-generic-password -w -s coire-admin-token`) and `COIRE_API` (default `http://127.0.0.1:8080`): subcommands `model add|list|show|job|update|load|retry|retire|delete`, `engines`, `engine unload`, `nodes`, `audit`; `chmod +x`; document at the top of the file

**Checkpoint**: `uv sync --all-packages` succeeds; `uv run pytest packages/coire-core` green; `./coire-up` on core mounts four secrets (`docker compose exec coire-api ls /run/secrets` shows `admin_token`); `install.sh --dry-run` lists the new paths.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Wire shapes, persistence, the admin gate, the typed node client, the node store and footprint primitives, the test doubles, and the simulated-mesh integration harness. Every story depends on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [ ] T008 [P] Create `packages/coire-core/src/coire_core/models/registry.py`: `ModelState`, `Visibility`, `Tag`, `PlacementPolicy` (validated `str` subtype with the regex from registry-api.yaml), `CapabilityProfile`, `Model`, `ModelCopy`, `ModelDetail`, `ModelListing`, `ModelAddRequest`, `ModelUpdateRequest` (rejects `capability_profile.verified`; accepts `chat_template: str | None`, ≤ 64 KiB), `ModelRejected`, `LoadRefused`, `slug_for(repo_id)`; `Model.chat_template` (D1 — Principle V); unit tests in `packages/coire-core/tests/test_registry_models.py` covering placement regex, tag enum, slug derivation, and the `verified` rejection
- [ ] T009 [P] Create `packages/coire-core/src/coire_core/models/jobs.py`: `DownloadStage`, `DownloadJob`, `JobKind`, `JobStage`, `JobStatus`, `RepoInspection` (+ `FileEntry`), `ChecksumManifest` (+ `ManifestFile`) with `canonical_bytes()` (sorted keys, no whitespace, files sorted by path) and `sha256()`; unit tests in `packages/coire-core/tests/test_manifest.py`: canonical form is order-independent, digest stable, `..` in a path rejected
- [ ] T010 [P] Create `packages/coire-core/src/coire_core/models/engine.py`: `EngineState`, `EngineProcess`, `EngineStatus`, `ReconcileExpectation`, `ReconcileRequest`, `ReconcileResult`, `BudgetRefused`; and `packages/coire-core/src/coire_core/models/audit.py`: `AuditOutcome`, `AuditRecord`; export all from `packages/coire-core/src/coire_core/models/__init__.py`
- [ ] T011 [P] Extend `NodeStatus` in `packages/coire-core/src/coire_core/models/node.py` with `engines: list[EngineStatus] = []`, `jobs: list[JobStatus] = []`, `memory_budget_bytes: int`, `memory_committed_bytes: int`, `store_free_bytes: int` (R11); update `apps/coire-node/tests/contract/test_node_health.py` and `apps/coire-node/src/coire_node/metrics.py` so the existing 000 contract test still validates against the amended `specs/000-bootstrap/contracts/health-api.yaml`
- [ ] T012 [P] Create `packages/coire-core/src/coire_core/memory.py`: pure `kv_bytes_per_token(inspection)`, `estimate_bytes(inspection, settings)`, `precision_label(inspection)` (`4bit-g64`, `8bit`, `bf16`, …), `fits_memory(estimate, nodes, settings) -> list[node]`, `fits_disk(total_bytes, nodes, settings) -> (ok, required, min_available)`; reads sizing keys from `text_config` when present (R2, R6, R12); unit tests in `packages/coire-core/tests/test_memory.py` including the Qwen3.8-27B-4bit worked example (≈ 26.2 GB) and the both-Studios disk rule
- [ ] T013 [P] Add `fallback: bool = True` to `MeshClient.__init__` in `packages/coire-core/src/coire_core/net.py`; when `False`, a mesh connect failure raises `MeshUnreachable` instead of retrying on egress (R3); add `stream(method, host, path, **kw)` returning the httpx streaming context; tests in `packages/coire-core/tests/test_net.py`: no egress attempt when `fallback=False`
- [ ] T014 Add ORM rows to `apps/coire-api/src/coire_api/db.py`: `ModelRow`, `ModelStateTransitionRow`, `ModelCopyRow` (unique `model_id,node_id`), `DownloadJobRow`, `EngineProcessRow` (`model_id` **nullable** — orphan rows from US4 have no model), `AuditRow`, with the enums as `SAEnum(values_callable=…)` like `NodeRow`, JSONB for `entitlement`, `tags`, `capability_profile`, `mismatched_paths`, `detail`; then write `apps/coire-api/alembic/versions/0002_registry.py` (`down_revision = "0001_nodes"`, `checkfirst` enum creation as in 0001, indexes on `models.repo_id` unique, `models.slug` unique, `models.state`, `download_jobs.stage`, `engine_processes.node_id`, `audit_log.at`); verify `alembic upgrade head` and `downgrade -1` against a local Postgres
- [ ] T015 Implement ADR-0004 in `apps/coire-api/src/coire_api/auth.py`: `PrincipalKind.ADMIN`; `require_principal` reads `HTTPBearer(auto_error=False)`, compares with `settings.admin_token` via `hmac.compare_digest` (empty configured token never matches), returns `Principal(kind=ADMIN, subject="admin-token")` or `ANONYMOUS`; `is_admin` true only for ADMIN; add `require_admin` dependency that raises 403 and writes an audit row with `outcome=refused` (uses T016); `CurrentAdmin = Annotated[Principal, Depends(require_admin)]`; unit tests in `apps/coire-api/tests/unit/test_auth.py`: correct token → admin; wrong/missing → anonymous; empty configured token → anonymous even if the header is empty
- [ ] T016 [P] Create `apps/coire-api/src/coire_api/audit.py`: `async def write_audit(session, *, actor, action, target_type, target_id, outcome, detail=None)` inserting `AuditRow` and flushing (never committing on its own); `detail` is passed through a `redact()` that drops any key containing `token`/`secret`/`password`; unit test in `apps/coire-api/tests/unit/test_audit.py` for redaction
- [ ] T017 [P] Create `apps/coire-api/src/coire_api/nodes_client.py`: `NodeClient(settings)` over `MeshClient` with the node's bearer from `settings.node_token_map`, one typed method per node-api.yaml operation (`inspect`, `list_models`, `delete_model`, `grant_export`, `revoke_export`, `start_pull`, `start_import`, `start_verify`, `get_job`, `cancel_job`, `list_engines`, `start_engine`, `get_engine`, `stop_engine`, `reconcile`), each parsing into the `coire-core` model and mapping 4xx/5xx to a `NodeError(kind, status, detail)`; unit tests in `apps/coire-api/tests/unit/test_nodes_client.py` with `httpx.MockTransport`
- [ ] T018 [P] Create `apps/coire-node/src/coire_node/store.py`: `Store(settings)` with `path_for(slug)` (rejects anything not matching the slug pattern; never joins caller paths), `manifest_path(slug)`, `read_manifest`/`write_manifest` (atomic), `hash_tree(slug, progress_cb) -> ChecksumManifest`, `list_copies() -> list[LocalCopy]`, `free_bytes()`, `delete(slug)`; unit tests in `apps/coire-node/tests/unit/test_store.py` with `tmp_path`: hashing matches `hashlib`, traversal rejected, delete removes manifest too
- [ ] T019 [P] Create `apps/coire-node/src/coire_node/footprint.py`: `resident_bytes(pid) -> int` using `ctypes` `libproc.proc_pid_rusage(pid, RUSAGE_INFO_V4, &buf)` → `ri_phys_footprint` on Darwin, `psutil.Process(pid).memory_info().rss` elsewhere (R6); unit test in `apps/coire-node/tests/unit/test_footprint.py`: for the current process the value is > 0 and, on Darwin, ≥ 80 % of `rss` (sanity, not equality)
- [ ] T020 [P] Create `apps/coire-node/src/coire_node/testing/fake_engine.py`: an `http.server`-based process (`python -m coire_node.testing.fake_engine --host H --port P --model PATH [--load-delay S] [--fail-on-start] [--allocate-mb N]`) serving `GET /health` → `{"status":"ok"}` immediately, `GET /v1/models`, and `POST /v1/chat/completions` that returns 503 until `--load-delay` has elapsed and then a one-token completion; `--fail-on-start` exits 3 with a traceback on stderr after 1 s; `--allocate-mb` holds a bytearray so footprint/RSS changes are measurable; refuses to start if `--model` does not exist (mirrors `mlx_lm.server`); unit test `apps/coire-node/tests/unit/test_fake_engine.py`
- [ ] T021 [P] Create `apps/coire-node/src/coire_node/testing/fake_hub.py`: a local HTTP fixture (pytest fixture `fake_hub` in `apps/coire-node/tests/conftest.py`) serving the Hub API shapes used by T026 — `GET /api/models/{repo}` with `siblings` (`rfilename`, `size`, `lfs.sha256` for weights only), `tags` (`mlx` on the MLX repo), `sha`, `gated` (true on `fake/gated`), 404 on `fake/missing` — and `GET /{repo}/resolve/{rev}/{path}` blobs (with `ETag`, `Range`) for three synthetic repos: `fake/mlx-tiny` (config.json with `quantization`, 3 safetensors ≈ 2 MB), `fake/raw-torch` (no quantization, no `mlx` tag), `fake/gguf-only`; `HF_ENDPOINT` points `huggingface_hub` at it
- [ ] T022 [P] Create `apps/coire-node/docker/node-test.Dockerfile` (CI only, never deployed): `python:3.13-slim` base, `uv sync --frozen --package coire-node --no-dev` **without** the `mlx` extras (Linux), plus `tini`; entrypoint `apps/coire-node/docker/node-test-entrypoint.sh` that loops `python -m coire_node` with a 1 s restart delay (a KeepAlive stand-in) so killing the agent process restarts it while `setsid` children survive; `COIRE_ENGINE_COMMAND` env selects the fake engine (T041); add the image name to the exclusion list in `scripts/image-policy.sh` and `scripts/pin-images.sh --check`, and assert in `tests/integration/test_topology.py` that no service of the production project uses it
- [ ] T023 Create `deploy/compose/compose.override.it.yaml`: network `coire-mesh-sim` (`internal: true`, subnet `192.168.100.0/24`); services `node-a` and `node-b` from T022 with `ipv4_address` `.11`/`.12`, `NODE_NAME`, `NODE_TOKEN` from the integration token map, `MESH_HOSTS_FILE=/etc/hosts` with `extra_hosts` for `coire-core.mesh`, `coire-edge-a.mesh`, `coire-edge-b.mesh`, `HF_TOKEN` passed only into `node-a` (`node-b` has no egress: it is only on `coire-mesh-sim`), tmpfs `/opt/coire`; attach `coire-api` to `coire-mesh-sim` at `.10`; update `tests/integration/conftest.py` to pass `-f compose.yaml -f compose.override.it.yaml`, wait for both nodes to appear `healthy` in `/health`, and expose `admin_headers()` and `api_url` fixtures; keep the production `compose.yaml` untouched (R9); the static `ipv4_address` entries are CI-only test harness — note this in the file header and confirm `test_topology.py` never loads the override

**Checkpoint**: `uv run pytest packages/coire-core apps/coire-api/tests/unit apps/coire-node/tests/unit` green; `alembic upgrade head` creates six tables; `COIRE_INTEGRATION=1 uv run pytest tests/integration/test_bringup.py` passes with two nodes registered from the simulated mesh (`/health` shows `coire-edge-a` and `coire-edge-b` healthy); `docker compose config` of the production project still matches `contracts/compose-topology.md`.

---

## Phase 3: User Story 1 — Admin adds a model and it becomes servable (Priority: P1) 🎯 MVP

**Goal**: `POST /api/v1/admin/models` inspects the repo on a Studio, rejects it before any bytes move if it is not MLX-format or fits nowhere, otherwise pulls once to the Studio with the most free disk, verifies against upstream digests, replicates to the peer over the mesh under a transfer grant, verifies file by file, and marks the model `ready` only when both copies verify — with progress observable throughout.

**Independent Test**: quickstart §1–§3 with `mlx-community/Qwen2.5-0.5B-Instruct-4bit`: reaches `ready` unattended with two verified copies (one `origin`, one `replica`); a truncated file on the origin mid-import leaves the model `failed` with the path recorded and the replica directory removed; a duplicate add is 409; a raw-torch repo is 422 `not_mlx_format`. In CI: `tests/integration/test_acquisition.py` against the simulated mesh.

### Tests for User Story 1

- [ ] T024 [P] [US1] Contract test `apps/coire-api/tests/contract/test_registry_api.py`: validate `ModelDetail`, `DownloadJob`, `ModelRejected` responses of `POST/GET /api/v1/admin/models`, `GET …/{id}`, `GET …/{id}/job`, `POST …/{id}/retry`, `DELETE …/{id}` against `specs/001-model-registry-node-agent/contracts/registry-api.yaml` using a `FakeSession` and a `FakeNodeClient`; and `test_openapi_matches_registry_contract` asserting every path in the contract exists in `app.openapi()` with the same methods and response codes
- [ ] T025 [P] [US1] Unit tests `apps/coire-api/tests/unit/test_state_machine.py` (every allowed `ModelState` transition records a reason; `ready` requires two verified copies; `replicating` never `ready` on one) and `apps/coire-api/tests/unit/test_placement.py` (`choose_origin` picks the node with the most `store_free_bytes`; tie → `coire-edge-a`; unreachable nodes excluded)
- [ ] T026 [P] [US1] Node contract tests `apps/coire-node/tests/contract/test_node_jobs.py` against `contracts/node-api.yaml` using `fake_hub` (T021) and `tmp_path` store: `inspect` on `fake/mlx-tiny` → `is_mlx_format: true`, sizes and `upstream_sha256` populated; `fake/raw-torch` → `false`; `fake/gated` → 423; `fake/missing` → 404; `pull` → job reaches `done` with a manifest whose digests match the blobs; re-`POST` with the same `job_id` → 200 unchanged; `pull` with `expected_total_bytes` > free → 507; `verify` after corrupting one file → `failed` with `mismatched_paths`
- [ ] T027 [P] [US1] Node contract tests `apps/coire-node/tests/contract/test_node_export_import.py`: two agent apps in one process on `127.0.0.1` ports (mesh app + egress app each); grant export on A, `import` on B pulls every file with Range and verifies → `done`; egress app returns 404 for `/node/export/*` even with the fallback header; expired/revoked grant → 404; a manifest mismatch → `failed`, partial directory removed (FR-009)
- [ ] T028 [P] [US1] Unit test `apps/coire-node/tests/unit/test_worker.py`: the worker writes progress to its state file at least once per file, restarts a `pull` from a state file whose stage is `transferring` and skips already-complete files (per-file resume, R5), and runs at `os.nice` ≥ 10
- [ ] T029 [P] [US1] Integration test `tests/integration/test_acquisition.py` (`@pytest.mark.integration`): add `mlx-community/Qwen2.5-0.5B-Instruct-4bit` via the admin route → poll `/job` until `done` (≤ 10 min) → model `ready`, two copies verified, exactly one `origin`; duplicate add → 409; `meta-llama/Llama-3.2-1B-Instruct` → 422 `not_mlx_format` and no row; with `DISK_RESERVE_BYTES=2T` on `coire-api` → 422 `no_fit_disk` with figures; the replica container's copy exists and was fetched from `coire-edge-a.mesh` (assert on `node-b` logs) and `node-b` never contacted the internet (it has no route); `coire_fallback_requests_total` on api did not increase

### Implementation for User Story 1

- [ ] T030 [P] [US1] Create `apps/coire-node/src/coire_node/hub.py`: `inspect(repo_id, revision, token) -> RepoInspection` via `HfApi.model_info(files_metadata=True)` + `hf_hub_download` of `config.json`/`tokenizer_config.json` into `node_hf_cache_dir`; MLX detection per R2 (`mlx` tag **or** `config.quantization`), GGUF-only detection, `text_config` fallback, `chat_template_present`; `snapshot(repo_id, revision, local_dir, token, progress_cb)` wrapping `snapshot_download(local_dir=…)` with `tqdm_class` progress; exceptions mapped `GatedRepoError → "gated"` (checked before `RepositoryNotFoundError → "not_found"`), `HfHubHTTPError → "network"`
- [ ] T031 [P] [US1] Create `apps/coire-node/src/coire_node/grants.py`: `Grants` registry (`grant → (slug, expires_at)`), `register`, `revoke_for(slug)`, `resolve(grant) -> slug | None` (expired = None), periodic sweep; unit test `apps/coire-node/tests/unit/test_grants.py`
- [ ] T032 [US1] Create `apps/coire-node/src/coire_node/worker.py` (`python -m coire_node.worker <state-file>`): loads `JobStatus` params, sets `os.nice(10)`, runs `pull` (snapshot → `hash_tree` → compare `upstream_sha256` → write manifest), `import` (fetch manifest from `/node/export/{grant}/manifest`, then each file via `MeshClient(fallback=False).stream` with `Range` from any existing partial, SHA-256 while writing, compare to manifest; on any mismatch delete the directory and write `failed` with `mismatched_paths`), `verify` (`hash_tree` vs stored or supplied manifest); writes the state file atomically after every stage and every 64 MiB; exit codes map to `error_kind`
- [ ] T033 [US1] Create `apps/coire-node/src/coire_node/jobs.py`: `JobSupervisor(settings, store)` — `start(kind, job_id, params)` idempotent on `job_id` (existing state file → return current status), one slug-writer lock, spawns T032 with `HF_TOKEN` only in the child env (T006), tracks `worker_pid`/CPU via psutil, `status(job_id)` reads the state file, `cancel(job_id)` (SIGTERM; keep partial pulls, delete partial imports), `resume_all()` on agent start re-spawns workers for state files in non-terminal stages (edge case 3), `active()` for `NodeStatus.jobs`
- [ ] T034 [US1] Create `apps/coire-node/src/coire_node/routes/jobs.py` (`POST /node/jobs/pull|import|verify`, `GET /node/jobs`, `GET/DELETE /node/jobs/{job_id}`; 507 when `store.free_bytes() < expected_total_bytes + disk_reserve_bytes`; 409 on slug-writer conflict) and `apps/coire-node/src/coire_node/routes/models.py` (`POST /node/models/inspect`, `GET /node/models`, `DELETE /node/models/{slug}` with 409 while an engine serves it, `POST/DELETE /node/models/{slug}/export`) — all behind `require_node_token`
- [ ] T035 [US1] Create `apps/coire-node/src/coire_node/routes/export.py` (`GET /node/export/{grant}/manifest`, `GET /node/export/{grant}/files/{path:path}` with `starlette.responses.FileResponse` honouring `Range`, path traversal rejected, 404 for any unknown/expired grant); mount it in `create_app` in `apps/coire-node/src/coire_node/agent.py` **only when `listener is NodePath.MESH`** (FR-007); wire T033's supervisor and T031's grants into `serve()`, call `resume_all()` at start, and add `jobs`, `store_free_bytes` to `metrics.py`'s sample (T011)
- [ ] T036 [P] [US1] Create `apps/coire-api/src/coire_api/registry/placement.py`: `choose_origin(nodes: list[NodeDetail]) -> NodeRow` (most `store_free_bytes` among `healthy` nodes; tie → `coire-edge-a`), `replica_for(origin, nodes)`, and `choose_load_node(policy, model, nodes)` for US3 (`single:<node>`/`pinned:<node>` → that node; `single:auto` → prefer `coire-edge-a` with budget headroom, else `coire-edge-b`)
- [ ] T037 [US1] Create `apps/coire-api/src/coire_api/registry/service.py`: `transition(session, model, to_state, reason)` (writes `ModelStateTransitionRow`, the only place `state` changes); `add_model(session, req, nodes_client, settings, actor)` — 409 on existing `repo_id`, pick an inspect node (any healthy studio), call `inspect`, reject 422 with `ModelRejected` for `not_mlx_format`/`gated`/`not_found`/`inspect_failed`, compute `precision`/`memory_estimate_bytes` (T012), check `fits_memory` and `fits_disk` → 422 with figures, else insert `ModelRow` in `downloading` + `DownloadJobRow(stage=inspect→pull, origin, replica)` + audit `model.add`; `retry_model` (only from `failed`; `attempt += 1`, stage reset to the earliest incomplete stage); `delete_model` (only from `failed`; issues `delete_model` to both nodes, removes rows); `recompute_state(session, model)` enforcing `ready ⇔ two verified copies`
- [ ] T038 [US1] Create `apps/coire-api/src/coire_api/registry/reconciler.py`: `RegistryReconciler(settings)` lifespan task (start/stop like `NodeProber`) that every `registry_reconcile_interval_s` loads unfinished `DownloadJobRow`s and advances each by stage — `pull`: `start_pull` (idempotent) then `get_job`, mirror bytes/files, on `done` upsert origin `ModelCopyRow(verified=True, role=origin)` and `Model.manifest_sha256`, → `verify_origin` (skipped: pull already verified against upstream; recorded as a transition reason) → `export`: generate `secrets.token_urlsafe(32)`, `grant_export` on origin, store on the job → `import`: `start_import` on replica with the manifest from the pull job → `get_job` until `done` → upsert replica copy `verified=True` → `verify_replica` (folded into import's file-by-file check) → `revoke_export`, `done`, `recompute_state` → `ready`, audit `model.ready`; any node `failed` → job `failed`, copy `mismatched_paths` recorded, model `failed` with reason; `NodeError(unreachable)` on the replica → stay in stage with `state_reason` set (edge case 4); trace span per stage; metrics `coire_model_state{state}` gauge and `coire_download_bytes_total`; register in `create_app` in `apps/coire-api/src/coire_api/app.py`
- [ ] T039 [US1] Create `apps/coire-api/src/coire_api/routes/admin_models.py` (`POST/GET /api/v1/admin/models`, `GET /api/v1/admin/models/{id}`, `GET …/{id}/job`, `POST …/{id}/retry`, `DELETE …/{id}`) using `CurrentAdmin` (T015), `ModelDetail` assembly with copies/job/engines, `ModelRejected` on 422, audit on every mutation; include the router in `app.py`; add `/api/v1/admin/nodes` to `apps/coire-api/src/coire_api/routes/admin_nodes.py` returning `NodeDetail` with the prober's last `NodeStatus` (extend `NodeProber` in `nodes_prober.py` to keep `latest_status[name]` in `app.state`)
- [ ] T040 [US1] Wire `HF_TOKEN` for the Linux integration job: `.github/workflows/ci.yml` `integration` step passes `HF_TOKEN: ${{ secrets.HF_TOKEN }}` **only** into the compose override's `node-a` environment (T023) and asserts via `docker compose config` that no other service carries it; document in the workflow that the ungated test model works without it and the secret is optional (operator creates it with `gh secret set HF_TOKEN` only if a gated test repo is ever needed)

**Checkpoint**: quickstart §1 passes on the real cluster *(cluster)* and `tests/integration/test_acquisition.py` passes in CI. Commit as the green MVP.

---

## Phase 4: User Story 2 — Only admins may acquire models (Priority: P1)

**Goal**: Every acquisition and curation route refuses a non-admin with 403 and an audit row and does nothing else; the Hugging Face credential exists only on the node agent.

**Independent Test**: quickstart §4 — every `/api/v1/admin/*` route without the admin token → 403, registry unchanged, one `refused` audit row per attempt; `docker inspect` of every container shows no HF token; `/run/secrets` in `coire-api` has no HF item.

### Tests for User Story 2

- [ ] T041 [P] [US2] Contract test `apps/coire-api/tests/contract/test_admin_guard.py`: enumerate every path under `/api/v1/admin/` from `app.openapi()` and, for each method, call it with no bearer and with a wrong bearer → 403 both times, `FakeSession` records exactly one `AuditRow(outcome=refused)` and no other insert; the correct bearer never yields 403 (may yield 404/422 with fake data); `GET /api/v1/admin/audit` returns `AuditRecord`s validated against the contract
- [ ] T042 [P] [US2] Integration test `tests/integration/test_admin_guard.py`: SC-002 over the running stack — unauthenticated `POST /api/v1/admin/models`, `DELETE`, `retire`, `load` → 403; `GET /api/v1/admin/models` (admin) count unchanged; audit rows present; FR-005: `docker inspect` every container of the `coire-it` project — no env var or mount containing `HF_`/`hf_` outside `node-a`; `docker compose exec coire-api ls /run/secrets` lacks any HF item; `grep -rIl 'hf_[A-Za-z0-9]\{20,\}' deploy apps packages` finds nothing

### Implementation for User Story 2

- [ ] T043 [US2] Add `GET /api/v1/admin/audit` (`limit`, `target_id` filters, newest first) to `apps/coire-api/src/coire_api/routes/admin_nodes.py`; ensure `require_admin` (T015) writes the `refused` row with `target_id` = the request path and commits it in its own session (a refused request has no route session to piggy-back on)
- [ ] T044 [US2] Add the FR-005 guard to `tests/integration/test_topology.py`: no service in the production `compose config` has an environment key or secret whose name contains `HF`; and to `scripts/image-policy.sh` rule 8: no first-party image layer contains a string matching `hf_[A-Za-z0-9]{20,}` (`docker history`/`crane`-free: run `strings` over the exported rootfs tar)

**Checkpoint**: SC-002 demonstrated in CI and on core; FR-004/FR-005 have durable guards.

---

## Phase 5: User Story 3 — Node agent loads, reports, and unloads an engine (Priority: P1)

**Goal**: A load starts `mlx_lm.server` bound to the mesh address in its own session, is reported `ready` only after it generates a token, exposes live per-process CPU and resident footprint alongside node CPU/GPU/thermal/memory/disk, refuses loads over budget, no-ops a duplicate load, reports a startup failure with exit status and output, and unloads cleanly releasing memory.

**Independent Test**: quickstart §5 *(cluster)* and `apps/coire-node/tests/engine/` on the macOS runner: `state` `starting → ready` only after generation; `resident_bytes` and `resident_delta_bytes` recorded; double load → 200 same engine; budget → 409; `chmod 000 config.json` → `failed` with `exit_output`; unload → process gone and `memory_free_bytes` within 2 %. In Linux CI the same verbs run against the fake engine (T020).

### Tests for User Story 3

- [ ] T045 [P] [US3] Node contract tests `apps/coire-node/tests/contract/test_node_engines.py` against `contracts/node-api.yaml` with `COIRE_ENGINE_COMMAND` pointing at the fake engine (T020) and a store copy from `fake_hub`: `POST /node/engines` → 202 `starting`, `ready` only after `--load-delay` elapses (assert `state` is still `starting` at `delay/2`), `pid` set, `process_create_time` set, engine's pgid == pid; second `POST` same slug → 200 same `engine_id`; `estimate_bytes` > budget → 409 `BudgetRefused` with figures; `--fail-on-start` → `failed` with `exit_code: 3` and traceback in `exit_output`, never `ready`; `DELETE` → `stopping` then `stopped`, pid gone, port reusable; `kill -9` the fake engine → `failed` within `node_engine_health_interval_s` + 1 s; `GET /node/health` carries `engines[]` with `cpu_percent` and `resident_bytes` and `memory_committed_bytes` == Σ estimates of live engines; **FR-017 argv test**: capture the spawned command (monkeypatch `subprocess.Popen`) and assert it is exactly `[…, "--model", store.path_for(slug), "--host", mesh, "--port", str(port), …]` with no request-derived string and `HF_HUB_OFFLINE=1` in the child env, and that a `chat_template` arrives as `--chat-template <file beside the copy>` never inline; **U3 concurrency**: two simultaneous `POST /node/engines` whose estimates each fit but together exceed the budget → exactly one 202 and one 409
- [ ] T046 [P] [US3] Contract test in `apps/coire-api/tests/contract/test_registry_api.py`: `POST /api/v1/admin/models/{id}/load` (202 `EngineProcess`, 200 when already loaded, 409 `LoadRefused` for `not_ready`/`budget`), `GET/DELETE /api/v1/admin/engines/{id}`, `GET /api/v1/admin/engines` — shapes and codes vs registry-api.yaml with a `FakeNodeClient`
- [ ] T047 [P] [US3] Engine tests `apps/coire-node/tests/engine/test_real_engine.py` (`@pytest.mark.engine`, skipped unless Darwin and `COIRE_ENGINE=1`): pull `mlx-community/Qwen2.5-0.5B-Instruct-4bit` into a temp store via `hub.snapshot`, start the agent with a `lo0` alias `192.168.100.11` as mesh address, load → `ready` only after the one-token probe (assert `/health` on the engine port returned 200 *before* `ready` — the R1 finding, so the test documents it), `resident_bytes` ≥ weight bytes, `lsof -nP -iTCP:<port>` shows only the mesh address, unload → `memory_free_bytes` back within 2 % (SC-006), external `kill -9` → `failed` within the interval (SC-009); record load seconds and footprint to `$GITHUB_STEP_SUMMARY` when present
- [ ] T048 [P] [US3] Integration test `tests/integration/test_engines_fake.py`: over the running stack, load the model from T029 → engine `ready` (fake engine in `node-a`), `GET /api/v1/admin/nodes` shows it under `engines` with `resident_bytes`; second load → 200 same id; unload → `stopped`; load with `NODE_MEMORY_BUDGET_FRACTION=0.000001` on `node-b` and `{"node":"coire-edge-b"}` → 409 `budget`

### Implementation for User Story 3

- [ ] T049 [US3] Create `apps/coire-node/src/coire_node/engines.py`: `EngineManager(settings, store, mesh_address)` — `allocate_port()` from the range (bind-test), `start(engine_id, slug, estimate_bytes, chat_template=None)` under a single `asyncio.Lock` spanning the budget check and the spawn (U3); `chat_template`, when given, is written to `<store>/<slug>.chat_template.jinja` and passed as `--chat-template` (D1) — (404 without a verified copy; 200 existing if a live engine serves the slug; 409 `BudgetRefused` when committed + estimate > budget; spawn `[sys.executable, "-m", "mlx_lm.server", "--model", store.path_for(slug), "--host", mesh_address, "--port", port, "--log-level", "INFO"]` — overridable by `COIRE_ENGINE_COMMAND` for tests — with `start_new_session=True`, `HF_HUB_OFFLINE=1`, stderr to a ring buffer of 4 KiB; record `(pid, create_time)`; write `engines.json` atomically), `_ready_probe()` task (poll `/health` until listening, then `POST /v1/chat/completions {"messages":[{"role":"user","content":"hi"}],"max_tokens":1}` until 200 or `node_engine_start_timeout_s` → `ready` with `load_seconds`, or `failed` with `exit_code`/`exit_output` if the process exits), `_health_loop()` every `node_engine_health_interval_s` (process alive? → else `failed`, "process exited"; refresh `cpu_percent` via psutil and `resident_bytes` via T019; `resident_delta_bytes`), `stop(engine_id)` (SIGTERM the process group, SIGKILL after 10 s, `stopped`, release port, rewrite `engines.json`), `list()`, `committed_bytes()`, `budget_bytes()`; OTel metrics `coire_engine_state`, `coire_engine_resident_bytes`, `coire_engine_load_seconds`
- [ ] T050 [US3] Create `apps/coire-node/src/coire_node/routes/engines.py` (`GET/POST /node/engines`, `GET/DELETE /node/engines/{engine_id}`) behind `require_node_token`; wire `EngineManager` into `serve()` in `agent.py`; add `engines`, `memory_budget_bytes`, `memory_committed_bytes` to `metrics.py`'s sample (T011); refuse `DELETE /node/models/{slug}` while `EngineManager` serves it (T034)
- [ ] T051 [US3] Extend `apps/coire-api/src/coire_api/registry/service.py` with `load_model(session, model, node_override, nodes_client, settings, actor)` (409 `not_ready` unless `ready`; `choose_load_node` (T036); if an `EngineProcessRow` in `starting`/`ready` exists for that node → return it 200; else insert row `starting`, `start_engine` on the node passing `model.chat_template` — 409 from the node → delete the row and return `LoadRefused(budget)`; audit `engine.load`) and `unload_engine(session, engine, nodes_client, actor)` (`stop_engine`, row → `stopping`, audit `engine.unload`); extend the reconciler (T038) with an **engine sync** pass: for every non-terminal `EngineProcessRow`, `get_engine` from the node and mirror `state`, `pid`, `process_create_time`, `resident_bytes`, `resident_delta_bytes`, `cpu_percent`, `last_health_at`, `state_reason`; `NodeError(404)` → `failed` "engine unknown to node"
- [ ] T052 [US3] Create `apps/coire-api/src/coire_api/routes/admin_engines.py` (`GET /api/v1/admin/engines`, `GET/DELETE /api/v1/admin/engines/{id}`) and add `POST /api/v1/admin/models/{id}/load` to `routes/admin_models.py`; include the router in `app.py`; `ModelDetail.engines` and `NodeDetail.engines` populated from `EngineProcessRow`
- [ ] T053 [US3] Add the `engine` CI job to `.github/workflows/ci.yml`: `runs-on: macos-15`, `uv sync --package coire-node`, `sudo ifconfig lo0 alias 192.168.100.11 255.255.255.0`, `COIRE_ENGINE=1 uv run pytest -m engine apps/coire-node/tests/engine -q`, `HF_TOKEN` optional; cache `~/.cache/huggingface` keyed on the test model; add `engine` to the `markers` list in `pyproject.toml` and make `test` (Linux) deselect it; **do not** add it to the branch-protection required checks until it has passed three times on `main` (note in the PR)

**Checkpoint**: T045 green on Linux with the fake engine; T047 green on `macos-15` with the real engine; quickstart §5 demonstrated on a Studio *(cluster)* with `resident_bytes` recorded against the estimate (SC-008).

---

## Phase 6: User Story 4 — Registry survives a node agent restart without lying (Priority: P2)

**Goal**: An agent restart re-adopts running engines by `(pid, create_time)` rather than killing them; an engine that died while the agent was down is corrected to `failed` with a recorded reason; a running engine matching no registry row is reported as an orphan and neither adopted nor killed.

**Independent Test**: quickstart §6 *(cluster)*: `launchctl kickstart -k` the agent with a model loaded → same pid still `ready`, engine still answers over the mesh, reconcile lists it under `adopted`; kill the engine while the agent is stopped → `failed` with reason; a hand-started `mlx_lm.server` → `orphan` row. In Linux CI: the same three cases via the node-test container's restart loop and the fake engine.

### Tests for User Story 4

- [ ] T054 [P] [US4] Unit tests `apps/coire-node/tests/unit/test_adoption.py`: `EngineManager.adopt_from_state()` with a synthetic `engines.json` — live pid with matching `create_time` (±1 s) and cmdline containing the store path → adopted `ready` after a fresh probe; pid gone → reported dead; pid reused by an unrelated process (different `create_time`) → dead, not adopted; a process found by `psutil.process_iter` whose cmdline has `mlx_lm.server`/`fake_engine` and a store path but no state entry → `orphan` with `engine_id: null`; `reconcile(expected)` returns `adopted`/`dead`/`orphans` per `ReconcileResult`
- [ ] T055 [P] [US4] Node contract test in `apps/coire-node/tests/contract/test_node_engines.py`: `POST /node/engines/reconcile` shape vs node-api.yaml; after starting a fake engine, constructing a **new** `EngineManager` over the same state dir (simulated restart) and reconciling with the expectation → `adopted` contains it and the process is still alive
- [ ] T056 [P] [US4] Integration test `tests/integration/test_agent_restart.py`: load a model on `node-a`; `docker compose exec node-a pkill -TERM -f 'coire_node$'` (entrypoint loop restarts the agent, T022); within 15 s the engine row is still `ready` with the **same pid** and `GET /api/v1/admin/audit` shows no `engine.load`; then stop the agent (`pkill -STOP`), kill the fake engine, resume (`pkill -CONT`) → row `failed` with `state_reason` "process gone during agent restart" and an audit row `engine.reconcile`; start a fake engine by hand in `node-a` with a store path → an `orphan` `EngineProcessRow` appears, `DELETE /api/v1/admin/engines/{id}` stops it

### Implementation for User Story 4

- [ ] T057 [US4] Add adoption to `apps/coire-node/src/coire_node/engines.py`: `adopt_from_state()` at construction (reads `engines.json`, validates each `(pid, create_time)` with ±1 s tolerance and cmdline check, re-attaches the health loop and re-runs the ready probe, marks dead entries and drops them from the file); `find_orphans()` via `psutil.process_iter(["pid","cmdline","create_time"])` filtering the engine command and the store prefix; `reconcile(expected: ReconcileRequest) -> ReconcileResult` (expected ∧ live → adopted; expected ∧ ¬live → dead; live ∧ ¬expected → orphans, recorded with `engine_id=None` so they appear in `engines[]` and count toward `memory_committed_bytes` using their measured footprint)
- [ ] T058 [US4] Add `POST /node/engines/reconcile` to `apps/coire-node/src/coire_node/routes/engines.py`; call `adopt_from_state()` before the listeners start in `serve()` so the first `/node/health` after a restart already carries the adopted engines
- [ ] T059 [US4] Extend the reconciler (T038/T051) in `apps/coire-api/src/coire_api/registry/reconciler.py` with `reconcile_node(node)`: build `expected` from non-terminal `EngineProcessRow`s for that node, call `reconcile`, then adopted → mirror `pid`/`create_time`/`state`; dead → `failed` with `state_reason="process gone during agent restart"` + audit `engine.reconcile`; orphans → insert `EngineProcessRow(state=orphan, model_id=None or matched by slug, node, port, pid)` if none exists for that `(node, pid)` (`model_id` is already nullable from T014); trigger it from `register_node` in `routes/nodes.py` (schedule via `app.state.reconciler.request_reconcile(name)`) and on every prober transition `unreachable → healthy`
- [ ] T060 [US4] Ensure unload of an `orphan` works end to end: `DELETE /api/v1/admin/engines/{id}` for an orphan row calls `stop_engine` with the node-side orphan id (the node assigns a transient id to orphans and keeps a map), row → `stopped`; audit `engine.unload` with `detail.orphan=true`

**Checkpoint**: SC-007 demonstrated on a Studio *(cluster)* and in `tests/integration/test_agent_restart.py`; US4 scenarios 1–3 each have a durable test.

---

## Phase 7: User Story 5 — Admin curates what exists and who may see it (Priority: P3)

**Goal**: Admins set visibility, entitlement, tags, description, capability profile, placement policy and idle TTL; the user-facing listing shows only `published`, `ready`, entitled models with live load state and no internal detail; unpublish hides immediately without unloading; retire unloads, deletes both copies, and keeps the row.

**Independent Test**: quickstart §7: `/api/v1/models` empty → publish → listed with `load_state` → unpublish → empty while the engine stays `ready` → retire → engines stopped, store directories gone on both Studios, row retained with transitions.

### Tests for User Story 5

- [ ] T061 [P] [US5] Contract tests in `apps/coire-api/tests/contract/test_registry_api.py`: `PATCH /api/v1/admin/models/{id}` (200 `ModelDetail`; 409 publishing a non-`ready` model; 422 on `capability_profile.verified`, a bad tag, a bad placement, a `chat_template` over 64 KiB; `chat_template` round-trips and `null` clears it), `POST …/{id}/retire` (202; 409 already retired), `GET /api/v1/models` returns `ModelListing` only (assert no `copies`, `store_path`, `state_reason` keys) — anonymous sees published+ready with empty entitlement; admin sees everything
- [ ] T062 [P] [US5] Unit test `apps/coire-api/tests/unit/test_listing.py`: `visible_to(principal, model)` — anonymous: published ∧ ready ∧ entitlement empty; admin: all; `load_state` derivation from engine rows (`loaded` if any `ready`, `loading` if any `starting`, else `cold`), `loaded_on` names, `estimated_warmup_seconds` = last recorded `load_seconds` or null
- [ ] T063 [P] [US5] Integration test `tests/integration/test_curation.py`: publish → anonymous listing contains it with `load_state`; unpublish → absent, engine still `ready`, copies present on both nodes (`GET /node/models` via admin nodes view); retire → engines `stopped`, `GET /node/models` on both nodes lacks the slug, model `retired`, `GET /api/v1/admin/models/{id}` still 200 with transitions in audit

### Implementation for User Story 5

- [ ] T064 [US5] Extend `apps/coire-api/src/coire_api/registry/service.py`: `update_model(session, model, req, actor)` (409 if `retired`; 409 if `visibility=published` and not `ready`; merge partial `capability_profile`; audit `model.update`, plus `model.publish`/`model.unpublish` when visibility changes) and `retire_model(session, model, nodes_client, actor)` (→ `retired` immediately with reason; enqueue: stop every engine of the model, then `delete_model(slug)` on both nodes, then mark copies deleted — driven by the reconciler as a `retire` pass so a node outage retries rather than half-completes; audit `model.retire`); `listing_for(principal, models, engines)` per T062
- [ ] T065 [US5] Add `PATCH /api/v1/admin/models/{id}` and `POST /api/v1/admin/models/{id}/retire` to `apps/coire-api/src/coire_api/routes/admin_models.py`; create `apps/coire-api/src/coire_api/routes/models.py` with `GET /api/v1/models` (depends on `CurrentPrincipal`, not `CurrentAdmin`) returning `list[ModelListing]`; include in `app.py`; add `model update` to `scripts/coire` (T007) taking a JSON body
- [ ] T066 [US5] Add the `retire` pass to the reconciler in `apps/coire-api/src/coire_api/registry/reconciler.py`: for `retired` models with copies not yet deleted or engines not `stopped`, drive stop → delete on each node with the same idempotent re-issue pattern as downloads; a node-side 409 (engine still serving) is retried after the stop completes

**Checkpoint**: quickstart §7 on the cluster *(cluster)* and `tests/integration/test_curation.py` in CI; the picker contract feature 003 reads (`ModelListing`) is stable.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Runbook, observability names, footprint verification, the full quickstart pass, and the first roster entry.

- [ ] T067 [P] Write `docs/runbooks/models.md`: add / watch progress / retry / delete a failed add; publish, unpublish, retire; load and unload; what an `orphan` is and how to clear it; where the store, manifests and state live on a Studio and what may be deleted by hand (nothing under `state/` while the agent runs); rotating `coire-hf-token` and `coire-admin-token`; how to read the audit log; link from `docs/runbooks/bootstrap.md`
- [ ] T068 [P] Verify the OTel surface: span names `registry.reconcile.<stage>`, `node.job.<kind>`, `node.engine.start|stop|probe`; metric names exactly as ADR-0003's extension lists them; add `apps/coire-api/tests/unit/test_metric_names.py` asserting the instruments exist with those names (so feature 009 can rely on them)
- [ ] T069 [P] Update `docs/ARCHITECTURE.md` §3.2/§3.3 and §10 with what shipped: the store path convention, transfer grants, per-file resume, the ready-probe definition, and `apps/coire-node` module layout; update `specs/000-bootstrap/quickstart.md` §5 footprint expectations (three new directories, second Keychain item)
- [ ] T070 Confirm the Studio footprint after install *(cluster)*: `apps/coire-node/uninstall.sh --dry-run` on both Studios lists exactly `/opt/coire/**`, the plist, and the two Keychain items; `brew list` and `ls /usr/local/bin` unchanged; `/opt/coire/envs/0.2.0/bin/python3 -c "import mlx_lm, huggingface_hub; print(mlx_lm.__version__)"` prints `0.31.3`; agent `collection_budget_ok: true` while a pull is running (R5)
- [ ] T071 Run the full `specs/001-model-registry-node-agent/quickstart.md` §1–§7 on the real cluster *(cluster)*, including the failure-injection table in §3 (truncated file, peer link down, Wi-Fi down mid-pull, reboot mid-pull) and the replication throughput measurement (R3); record every figure in the PR description per Principle VII
- [ ] T072 Add the first production roster entry *(cluster, after merge)*: `scripts/coire model add mlx-community/Qwen3.8-27B-8bit` then `-4bit`, tags `coding,reasoning`, `placement_policy: single:auto`; record `memory_estimate_bytes` vs measured `resident_bytes` for each in `specs/001-model-registry-node-agent/research.md` R12 as the first drift data point (user decision 2026-08-30)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies; T002–T007 parallel after T001
- **Foundational (Phase 2)**: depends on Phase 1; **blocks all stories**. T008–T013, T016–T022 parallel; T014 needs T008–T010; T015 needs T016; T023 needs T022 and T003
- **US1 (Phase 3)**: depends on Phase 2. Node side T030–T035 and core side T036–T039 are independent of each other until T038 (reconciler) needs T017's client to talk to T034/T035's routes; T029 needs everything in the phase plus T023
- **US2 (Phase 4)**: depends on Phase 2 and on US1's routes existing (T039); T041 can be written against T015 alone
- **US3 (Phase 5)**: depends on Phase 2 and T036; **independent of US1/US2** except T048 which needs a `ready` model (T029)
- **US4 (Phase 6)**: depends on US3 (T049, T051)
- **US5 (Phase 7)**: depends on US1 (rows) and US3 (engines, for `load_state` and retire)
- **Polish (Phase 8)**: T067–T069 any time after their subjects exist; T070–T072 last, on the cluster

### User Story Dependencies

- **US1 (P1)**: after Phase 2 — no story dependencies; **MVP**
- **US2 (P1)**: after US1's routes — hardens them; independent of US3–US5
- **US3 (P1)**: after Phase 2 — independent of US1 for the node side; the core-side load route needs a `ready` model only to be *exercised*, not to be built
- **US4 (P2)**: after US3
- **US5 (P3)**: after US1 and US3

### Within Each User Story

- Tests first, failing, then implementation
- `coire-core` shapes → node modules → node routes → core service → reconciler → core routes → integration test
- Cluster verification last

### Parallel Opportunities

- Phase 1: T002, T003, T004, T005, T006, T007 together after T001
- Phase 2: T008, T009, T010, T011, T012, T013, T016, T017, T018, T019, T020, T021, T022 together; then T014, T015; then T023
- US1: T024–T029 together (tests); then T030, T031, T036 together; T032 → T033 → T034 → T035; T037 → T038 → T039; T040 alongside
- US3: T045–T048 together; T049 → T050; T051 → T052; T053 alongside
- **US1 and US3 can be built by two people concurrently after Phase 2** — the only shared file is `metrics.py` (T035 adds `jobs`/`store_free_bytes`, T050 adds `engines`/budget), sequenced by story order
- US4: T054, T055, T056 together; T057 → T058; T059 → T060
- US5: T061, T062, T063 together; T064 → T065 → T066
- Phase 8: T067, T068, T069 together

---

## Parallel Example: User Story 1

```bash
# Tests first, all six together (they must fail until T030–T039 land):
Task: "Registry contract test in apps/coire-api/tests/contract/test_registry_api.py"
Task: "State machine + placement unit tests in apps/coire-api/tests/unit/"
Task: "Node jobs contract test in apps/coire-node/tests/contract/test_node_jobs.py"
Task: "Export/import contract test in apps/coire-node/tests/contract/test_node_export_import.py"
Task: "Worker unit test in apps/coire-node/tests/unit/test_worker.py"
Task: "Acquisition integration test in tests/integration/test_acquisition.py"

# Then the three independent leaves together:
Task: "Hub wrappers in apps/coire-node/src/coire_node/hub.py"
Task: "Transfer grants in apps/coire-node/src/coire_node/grants.py"
Task: "Placement in apps/coire-api/src/coire_api/registry/placement.py"
```

## Parallel Example: User Story 3

```bash
# All four tests together:
Task: "Node engines contract test in apps/coire-node/tests/contract/test_node_engines.py"
Task: "Load/unload contract test in apps/coire-api/tests/contract/test_registry_api.py"
Task: "Real engine test in apps/coire-node/tests/engine/test_real_engine.py"
Task: "Engines integration test in tests/integration/test_engines_fake.py"

# Node side and core side concurrently:
Task: "EngineManager in apps/coire-node/src/coire_node/engines.py"          # then routes/engines.py
Task: "load/unload service in apps/coire-api/src/coire_api/registry/service.py"  # then routes/admin_engines.py
Task: "engine CI job in .github/workflows/ci.yml"
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 → Phase 2 → Phase 3 (T024–T040)
2. **STOP and VALIDATE**: `tests/integration/test_acquisition.py` green in CI; quickstart §1 on the cluster shows two verified copies. This alone satisfies the roadmap's "`ready` implies two verified copies"
3. Merge-able as a first PR if the branch is getting long — US2's guard already exists from Phase 2 (T015), so the MVP is not an open write surface

### Incremental Delivery

1. Phase 1 + 2 — foundation, nothing user-visible yet; CI green with two simulated nodes
2. US1 — acquisition end to end → MVP
3. US3 — load/unload against the real engine on the macOS runner and a Studio → the roadmap's "`load`/`unload` work"
4. US2 — the SC-002 guard test and the FR-005 leak guards → the roadmap's "a user key gets 403"
5. US4 — adoption → the roadmap's "registry reflects true process state after a node-agent restart"
6. US5 — curation → what features 003 and 008 read
7. Phase 8 — runbook, quickstart pass, Qwen3.8-27B added

### Parallel Team Strategy

After Phase 2: one person on US1 (node jobs + reconciler), one on US3 (engines); US2 and US4 follow their parents; US5 last. Phase 8's cluster tasks need the operator.

---

## Notes

- Every `POST /node/jobs/*` and `POST /node/engines` is idempotent on the caller's id; the reconciler relies on that after a restart — do not "optimise" it away (ADR-0005)
- `/node/export/*` exists only on the mesh app (T035). The egress app returning 404 there is a tested invariant (T027), not an omission
- `engines.json` and `jobs/*.json` are caches. If the registry and a node disagree, the reconcile result wins and is audited — never edit those files to "fix" state
- The macOS `engine` job is informational until it has been green three times on `main`; it is then added to the required checks (T053)
- Tasks marked *(cluster)* need the real Studios and 000's T063 install; everything else runs in CI
