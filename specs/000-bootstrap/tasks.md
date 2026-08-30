---
description: "Task list for feature 000 — Bootstrap Control Plane Skeleton"
---

# Tasks: Bootstrap Control Plane Skeleton

**Input**: Design documents from `/specs/000-bootstrap/`

**Prerequisites**: plan.md, spec.md, research.md (R1–R13), data-model.md, contracts/ (health-api.yaml, compose-topology.md, image-policy.md), quickstart.md

**Tests**: INCLUDED. Constitution Principle VII requires contract tests for every API surface and an integration test per feature; the spec's SC-004…SC-008 are CI checks by definition. Test tasks precede implementation within each story and must fail first.

**Organization**: Tasks are grouped by user story. Stories US1 and US2 share the compose project but are independently verifiable (US1 = comes up; US2 = stays up while one piece restarts). US3 (CI) and US4 (node agent) touch disjoint files and can proceed in parallel with each other and with US1/US2 once Phase 2 is done.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1–US4 from spec.md
- Every task names exact file paths

## Path Conventions

`uv` workspace monorepo per plan.md: `packages/coire-core/`, `apps/coire-api/`, `apps/coire-node/`, `apps/coire-agent/`, `apps/coire-web/`, `deploy/{compose,cluster,launchd}/`, `.github/workflows/`, `docs/runbooks/`. Python source under `src/<package>/`, tests under each package's `tests/`; cross-cutting integration tests under repo-root `tests/integration/`.

**Pinning rule (applies to every Dockerfile and compose task)**: every `FROM` and every compose `image:` carries `@sha256:`; resolve digests at task time with `docker buildx imagetools inspect <ref> --format '{{json .Manifest.Digest}}'` and record them in `deploy/compose/images.lock` (one line per image: `name ref@sha256:…`). Bare tags fail `tests/integration/test_topology.py` and CI rule 7.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Workspace skeleton, toolchain pins, lint/type configuration. Nothing runs yet.

- [X] T001 Create the `uv` workspace root: `pyproject.toml` with `[tool.uv.workspace] members = ["packages/*", "apps/coire-api", "apps/coire-node", "apps/coire-agent"]`, `requires-python = "==3.13.*"`, and `.python-version` containing `3.13`; run `uv lock` and commit `uv.lock`
- [X] T002 [P] Create package skeletons with `pyproject.toml` + empty `src/<pkg>/__init__.py` + `tests/__init__.py` for `packages/coire-core` (`coire_core`), `apps/coire-api` (`coire_api`, `coire_mcp`, `coire_scheduler` as three modules under one project), `apps/coire-node` (`coire_node`), `apps/coire-agent` (`coire_agent`); each declares `coire-core` as a workspace dependency where applicable
- [X] T003 [P] Configure `ruff` (format + check, `target-version = "py313"`, select `E,F,I,B,UP,ASYNC`) and `mypy --strict` in root `pyproject.toml` `[tool.ruff]` / `[tool.mypy]`; add `.pre-commit-config.yaml` running `ruff format`, `ruff check --fix`, `mypy`
- [X] T004 [P] Scaffold `apps/coire-web/` with Vite + React + TypeScript (`package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `src/main.tsx`, `src/App.tsx` rendering a placeholder), plus `eslint.config.js`, `.prettierrc`, and `vitest` config; commit the lockfile
- [X] T005 [P] Create empty directories with `.gitkeep`: `deploy/compose/`, `deploy/cluster/`, `deploy/launchd/`, `.github/workflows/`, `tests/integration/`, `scripts/`
- [X] T006 [P] Add `docs/runbooks/bootstrap.md` skeleton with the five headings the constitution requires: Bring up, Bring down, Restart one service, Where to look, Roll back (to be filled in T068)

**Checkpoint**: `uv sync --all-packages` succeeds; `uv run ruff check` and `uv run mypy` pass on empty packages; `npm ci && npm run build` succeeds in `apps/coire-web`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared wire shapes, settings, the auth seam, the DB migration, the app factory with OTel, the distroless build pattern, and mesh name resolution. Every user story depends on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T007 [P] Implement `packages/coire-core/src/coire_core/models/health.py`: `ServiceHealth`, `HealthResponse` (status enum healthy/degraded/unhealthy), `ReadyResponse` — Pydantic v2, `ConfigDict(extra="forbid")`, fields exactly as in data-model.md and contracts/health-api.yaml
- [X] T008 [P] Implement `packages/coire-core/src/coire_core/models/node.py`: `NodeRole`, `Reachability`, `ThermalState`, `NodePath` enums; `NodeRegistration` (token as `SecretStr`, `mesh_address` validated within `192.168.100.0/24`), `Node`, `NodeStatus` — per data-model.md
- [X] T009 [P] Implement `packages/coire-core/src/coire_core/settings.py`: pydantic-settings `Settings` reading `postgres_password`, `key_signing_secret`, `node_tokens` from `/run/secrets/<name>` (a `SecretsDir` source), `POSTGRES_HOST`, `OTLP_ENDPOINT` (default `http://otel-collector:4317`), `NODE_PROBE_INTERVAL_S=10`, `NODE_COLLECTION_BUDGET_CPU_PCT=2.0`, `NODE_COLLECTION_BUDGET_RSS_BYTES=157286400`; `database_url` assembled property
- [X] T010 [P] Implement `packages/coire-core/src/coire_core/net.py`: `MeshClient` (httpx.AsyncClient wrapper) that resolves `<host>.mesh` first and, on connect error, retries `<host>.local` with header `X-Coire-Path: fallback`, incrementing an OTel counter `coire_fallback_requests_total{peer}` and logging WARNING (research R9)
- [X] T011 [P] Write unit tests for T007–T010 in `packages/coire-core/tests/test_models.py` (extra-field rejection, mesh_address range, enum values) and `packages/coire-core/tests/test_net.py` (fallback path taken only on connect failure; counter increments; header present only on fallback) using `httpx.MockTransport`
- [X] T012 [P] Implement `apps/coire-api/src/coire_api/auth.py`: `Principal` model (`kind: Literal["anonymous"]`) and `async def require_principal() -> Principal` returning anonymous; docstring cites ADR-0001 and states that feature 007 replaces the body without changing the signature
- [X] T013 Implement `apps/coire-api/src/coire_api/db.py` (async engine + session factory from `Settings.database_url`) and Alembic: `apps/coire-api/alembic.ini`, `alembic/env.py` (async), `alembic/versions/0001_nodes.py` creating table `nodes` with columns from data-model.md `Node` (unique `name`, enum `reachability`, timestamps)
- [X] T014 Implement `apps/coire-api/src/coire_api/app.py`: `create_app()` FastAPI factory; OpenTelemetry SDK with OTLP gRPC exporter to `Settings.otlp_endpoint`, `FastAPIInstrumentor`, resource `service.name=coire-api`, `service.version` from package metadata; exporter failures logged, never raised (research R11/R13); global dependency `require_principal` on the router
- [X] T015 [P] Implement stub apps `apps/coire-api/src/coire_mcp/main.py` and `apps/coire-api/src/coire_scheduler/main.py`: FastAPI with only `GET /ready` returning `ReadyResponse(service="coire-mcp"|"coire-scheduler")`, same OTel wiring as T014, ports 8001 / 8002
- [X] T016 Write `apps/coire-api/docker/base.Dockerfile` implementing research R1 as a reusable pattern: stage `builder` FROM `python:3.13-slim-bookworm@sha256:…` with `COPY --from=ghcr.io/astral-sh/uv:0.12.7@sha256:… /uv /bin/uv`, `uv sync --frozen --no-dev --package <name>` into `/app/.venv` (`--relocatable`); final stage FROM `gcr.io/distroless/base-debian12:nonroot@sha256:…`, `COPY --from=builder /usr/local /usr/local`, `COPY --from=builder /app /app`, `USER 65532`, `WORKDIR /app`, exec-form `ENTRYPOINT ["/app/.venv/bin/python3"]`; document the pinned digests in `deploy/compose/images.lock`
- [X] T017 [P] Create `deploy/cluster/hosts` (`192.168.100.10 coire-core.mesh` / `.11 coire-edge-a.mesh` / `.12 coire-edge-b.mesh`) and `deploy/cluster/nodes.yaml` (declared inventory: `coire-edge-a` role studio gpu_cores 80, `coire-edge-b` role studio gpu_cores 60 — names and roles only, no addresses)
- [X] T018 [P] Write `scripts/apply-mesh-hosts.sh`: idempotently replaces the `# BEGIN coire-mesh` … `# END coire-mesh` block in `/etc/hosts` from `deploy/cluster/hosts` (requires sudo once; prints a diff; `--check` mode exits non-zero if out of date) per ADR-0002
- [X] T019 [P] Write `apps/coire-web/healthcheck/main.go` (static probe, `CGO_ENABLED=0`, no deps: default mode GETs a URL and exits 0 on 2xx else 1; `--tcp host:port` mode exits 0 if a TCP connect succeeds within 2 s else 1, so topology tests can distinguish "no route" from "not HTTP") and `apps/coire-web/healthcheck/go.mod` — research R2

**Checkpoint**: `uv run pytest packages/coire-core` green; `uv run alembic -c apps/coire-api/alembic.ini upgrade head` works against a local Postgres; `docker build -f apps/coire-api/docker/base.Dockerfile --target builder .` succeeds; `go build ./apps/coire-web/healthcheck` produces a static binary.

---

## Phase 3: User Story 1 — Operator brings the whole control plane up on core (Priority: P1) 🎯 MVP

**Goal**: One documented command (`coire-up`) brings eight containers up on five networks with Keychain-sourced secrets that never touch disk, and `/health` through nginx returns 200 with every service healthy.

**Independent Test**: quickstart §1 — `time ./coire-up` completes ≤ 3 min; `curl http://127.0.0.1:8080/health | jq .status` → `"healthy"`; `docker compose ps` shows `coire-migrate` exited 0; the negative case (a missing Keychain item) aborts naming the item and starts nothing.

### Tests for User Story 1

- [X] T020 [P] [US1] Contract test `apps/coire-api/tests/contract/test_health_api.py`: for `/ready` and `/health` on `create_app()` via `httpx.ASGITransport`, assert response bodies validate against the schemas in `specs/000-bootstrap/contracts/health-api.yaml` (load YAML, resolve `$ref`, use `jsonschema`); assert 503 when the Postgres probe is stubbed to fail; assert 200 + `degraded` when only `coire-mcp` probe fails
- [X] T021 [P] [US1] Contract test `apps/coire-api/tests/contract/test_openapi_matches.py`: the paths `/ready`, `/health`, `/api/v1/nodes/register` in `app.openapi()` are schema-equal to `contracts/health-api.yaml` (this is the "OpenAPI generated, reviewed shape enforced" check from the plan's Principle III row)
- [X] T022 [P] [US1] Integration test `tests/integration/test_bringup.py` (marked `integration`, skipped unless `COIRE_INTEGRATION=1`): runs `deploy/compose/coire-up --secrets-from-env` (see T032), polls `http://127.0.0.1:8080/health` for ≤ 180 s until `status == healthy`, asserts every expected `services[].name` present and healthy, asserts `docker compose ps --format json` shows `coire-migrate` `exited` with code 0; a second test launches two `coire-up` processes concurrently and asserts one exits 3 with `bring-up already running` and `alembic_version` has exactly one row; then `coire-down`

### Implementation for User Story 1

- [X] T023 [US1] Implement `apps/coire-api/src/coire_api/routes/health.py`: `GET /ready` → `ReadyResponse`; `GET /health` → aggregate per research R11: Postgres `SELECT 1`, `coire-mcp:8001/ready`, `coire-scheduler:8002/ready`, `otel-collector:13133/` (each with 2 s timeout, `latency_ms` recorded), plus one `ServiceHealth` per row in `nodes` derived from `reachability`; `status` = unhealthy if Postgres fails (HTTP 503), degraded if any other fails, else healthy
- [X] T024 [US1] Implement `apps/coire-api/src/coire_api/routes/nodes.py`: `POST /api/v1/nodes/register` accepting `NodeRegistration`; 403 if `name` not in `deploy/cluster/nodes.yaml` (loaded at startup from `/app/nodes.yaml`, copied into the image), 401 if `token` ≠ `Settings.node_tokens[name]` (constant-time compare), 422 on validation; upsert on `name`; returns `Node`; depends on `require_principal`
- [X] T025 [US1] Register both routers in `create_app()` (`apps/coire-api/src/coire_api/app.py`) and add `apps/coire-api/src/coire_api/__main__.py` running uvicorn on `0.0.0.0:8000` (no reload, `--proxy-headers`)
- [X] T026 [US1] Write the four image Dockerfiles from the T016 pattern: `apps/coire-api/docker/api.Dockerfile` (`CMD ["-m","coire_api"]`, `HEALTHCHECK CMD ["/app/.venv/bin/python3","-c","import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=2).status==200 else 1)"]`), `mcp.Dockerfile` (`-m coire_mcp`, :8001), `scheduler.Dockerfile` (`-m coire_scheduler`, :8002), `migrate.Dockerfile` (`CMD ["-m","alembic","-c","/app/alembic.ini","upgrade","head"]`, no HEALTHCHECK); each copies only the packages it needs (`uv sync --package`)
- [X] T027 [P] [US1] Write `apps/coire-web/nginx/nginx.conf`: listen 8080, `root /usr/share/nginx/html` for the SPA with SPA fallback, `location ~ ^/(health|ready|api/)` → `proxy_pass http://coire-api:8000` with `proxy_buffering off` and `proxy_read_timeout 3600s`, `location /mcp/` → `http://coire-mcp:8001` with the same streaming settings, `location /v1/` reserved (returns 404 until feature 003), and a **local, non-proxied** `location = /nginx-health { return 200 "ok"; }` used by the container healthcheck so an api restart never marks web unhealthy — buffering off on all proxied paths (FR-008)
- [X] T028 [P] [US1] Write `apps/coire-web/Dockerfile`: stage `web` FROM `node:22-bookworm-slim@sha256:…` running `npm ci && npm run build`; stage `probe` FROM `golang:1.23-bookworm@sha256:…` building T019 static; final FROM `cgr.dev/chainguard/nginx@sha256:…`, `COPY --from=web /app/dist /usr/share/nginx/html`, `COPY --from=probe /healthcheck /healthcheck`, `COPY nginx/nginx.conf /etc/nginx/nginx.conf`, `HEALTHCHECK CMD ["/healthcheck","http://127.0.0.1:8080/nginx-health"]` (local path, never the proxied `/ready`)
- [X] T029 [P] [US1] Write `apps/coire-agent/Dockerfile` (T016 pattern for `coire_agent`, `CMD ["-m","coire_agent"]`) and `apps/coire-agent/src/coire_agent/__main__.py` printing `coire-agent <version>` and exiting 0 — built in CI, never started on core (FR-017)
- [X] T030 [P] [US1] Write `deploy/compose/otel-collector.yaml`: OTLP gRPC + HTTP receivers, `health_check` extension on `:13133`, `debug` exporter at `basic` verbosity, memory_limiter processor (256 MiB); and `deploy/compose/otel.Dockerfile` FROM `otel/opentelemetry-collector-contrib@sha256:…` + `COPY --from=<web image probe stage> /healthcheck /healthcheck` + `HEALTHCHECK CMD ["/healthcheck","http://127.0.0.1:13133/"]` (research R2/R11: upstream image has no shell, so it gets the same static probe)
- [X] T031 [US1] Write `deploy/compose/compose.yaml` exactly per `contracts/compose-topology.md`: 8 services, 5 networks (`internal: true` on all but `coire-edge`), `coire-web` `ports: ["127.0.0.1:8080:8080"]` only; `depends_on` with `service_healthy` / `service_completed_successfully` as specified; every first-party service `user: "65532:65532"`, `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `restart: unless-stopped`; `postgres` with named volume `coire-pgdata`, `POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password`, `pg_isready` healthcheck; `docker-socket-proxy` with `/var/run/docker.sock:ro`, env `CONTAINERS=1 IMAGES=1 POST=1` and all others `0`; secrets `postgres_password`, `key_signing_secret`, `node_tokens` declared with `environment:` sources `COIRE_SECRET_POSTGRES_PASSWORD`, `COIRE_SECRET_KEY_SIGNING_SECRET`, `COIRE_SECRET_NODE_TOKENS` (research R4); all images by digest from `images.lock`
- [X] T032 [US1] Write `deploy/compose/coire-up` (bash, `set -euo pipefail`): for each of `coire-postgres-password`, `coire-key-signing-secret`, `coire-node-tokens` read `security find-generic-password -w -s <name>` into the matching `COIRE_SECRET_*` variable, aborting with `missing keychain item: <name>` and exit 2 before touching compose if any is absent; `--secrets-from-env` flag skips Keychain and requires the variables already set (for CI); takes an exclusive non-blocking `flock` on `deploy/compose/.coire-up.lock` and exits 3 with `bring-up already running` if held (spec edge case: concurrent operators must not race the migration); runs `scripts/apply-mesh-hosts.sh --check` (warn only on core), then `docker compose up -d --wait --wait-timeout 180`; never writes a secret to disk; exit code propagates
- [X] T033 [P] [US1] Write `deploy/compose/coire-down` (`docker compose down`; `--purge` also `-v` removing `coire-pgdata` after an explicit `y` confirmation)
- [X] T034 [US1] Write `deploy/compose/images.lock` and a `scripts/pin-images.sh` that resolves every image reference used in `compose.yaml` and all Dockerfiles to its current digest and rewrites them (`--check` mode fails if any reference is unpinned) — run it and commit the result; first-party entries point at `ghcr.io/mcteer/coire/<name>@sha256:…` as pushed by CI (T046). Also write `deploy/compose/compose.override.dev.yaml` with `build:` blocks for the six first-party images and `coire-otel`, so local iteration on core is `docker compose -f compose.yaml -f compose.override.dev.yaml up --build`; `coire-up` never loads the override (production path pulls pinned images only)

**Checkpoint**: quickstart §1 passes on core, including the negative case. Commit as a green MVP.

---

## Phase 4: User Story 2 — Operator restarts one service without disturbing the others (Priority: P1)

**Goal**: Restarting any single container leaves every other service healthy throughout, dependents reconnect on their own, and `coire-web` is back in under 5 s — with the topology invariants enforced by a test.

**Independent Test**: quickstart §2 and §3 — restart each of six services in turn while polling `/health` once per second for 30 s and observe no *other* service ever unhealthy; `time docker compose restart coire-web` < 5 s; `pytest tests/integration/test_topology.py` green; `coire-web` cannot reach `postgres`.

### Tests for User Story 2

- [X] T035 [P] [US2] Integration test `tests/integration/test_topology.py` (no running stack needed): parse `docker compose -f deploy/compose/compose.yaml config --format json` and assert every invariant in `contracts/compose-topology.md`: network membership matrix, `coire-web` ∉ `coire-db`, `coire-docker` members == {`coire-scheduler`, `docker-socket-proxy`}, healthcheck on every long-lived service, exact `depends_on` conditions, nobody depends on `otel-collector`, first-party hardening keys present, all `image:` values contain `@sha256:`, all secrets have `environment:` sources and none have `file:`, exactly five networks with `internal: true` on all but `coire-edge`, only `127.0.0.1:8080` published, no service uses a `coire-agent` image (FR-017), and `coire-web`'s healthcheck targets `/nginx-health` rather than a proxied path
- [X] T036 [P] [US2] Integration test `tests/integration/test_restart_isolation.py` (`COIRE_INTEGRATION=1`): with the stack up, for each of `coire-api`, `postgres`, `coire-mcp`, `coire-scheduler`, `otel-collector`, `docker-socket-proxy`: run `docker compose restart <svc>`, sample `/health` every 1 s for 30 s, assert no `services[]` entry other than `<svc>` (and, for `postgres`, `api`'s own status flipping to unhealthy is allowed for ≤ 10 s) reports `healthy: false`; assert `coire-web` restart returns `/ready` 200 within 5 s; assert `docker compose stop coire-mcp` leaves `/ready` on api at 200 and `/health` at `degraded`; assert `docker compose exec coire-web /healthcheck --tcp postgres:5432` exits non-zero **and** a TCP connect from `coire-api` to `postgres:5432` succeeds (proves the failure is "no route", not "not HTTP")
- [X] T037 [P] [US2] Unit test `apps/coire-api/tests/unit/test_db_reconnect.py`: with the engine configured per T038, a connection failure followed by recovery results in the next `/health` Postgres probe succeeding without process restart (use `pool_pre_ping` behaviour under a failing then succeeding fake DBAPI)

### Implementation for User Story 2

- [X] T038 [US2] Harden `apps/coire-api/src/coire_api/db.py`: `pool_pre_ping=True`, `pool_recycle=300`, connect timeout 5 s, and make the `/health` Postgres probe use a fresh connection with a 2 s statement timeout so a restarting Postgres yields a fast `unhealthy` and an automatic recovery (spec US2 scenario 2)
- [X] T039 [US2] Make the `/health` dependency probes in `apps/coire-api/src/coire_api/routes/health.py` run concurrently (`asyncio.gather`, individual timeouts) so one slow dependency cannot make `/health` itself slow, and ensure a failed probe of `coire-mcp`/`coire-scheduler`/`otel-collector` degrades rather than fails (SC-002 "stopping coire-mcp leaves chat-path health unaffected")
- [X] T040 [US2] Tune compose for fast, isolated restarts in `deploy/compose/compose.yaml`: `stop_grace_period: 5s` on first-party services, healthcheck `interval: 5s timeout: 2s retries: 3 start_period: 10s` (nginx `start_period: 2s`), and `restart: unless-stopped` everywhere so a restarted dependency never cascades
- [X] T041 [US2] Verify and record: run quickstart §2 on core with secrets sourced from Keychain, confirm `docker compose restart <svc>` works with no secret file on disk (research R4), and paste the timing table into `docs/runbooks/bootstrap.md` under "Restart one service"

**Checkpoint**: US1 and US2 both green on core. `tests/integration/test_topology.py` is the durable guard on the topology contract.

---

## Phase 5: User Story 3 — CI rejects an image that violates the container rules (Priority: P2)

**Goal**: Every pull request builds all six first-party images natively on arm64, scans them, publishes SBOMs, mechanically enforces the seven image-policy rules, and runs lint and tests; a deliberately-introduced shell fails with a message naming the rule.

**Independent Test**: quickstart §4 — `gh run watch` shows `build`, `scan`, `sbom`, `image-policy`, `lint`, `test`, `integration` green on `ubuntu-24.04-arm`; the SC-008 fixture branch fails `image-policy` with `policy: shell present in coire-api: bin/sh`.

### Tests for User Story 3

- [X] T042 [P] [US3] Test `tests/unit/test_image_policy_script.py`: builds two tiny fixture images from `tests/fixtures/policy/{good,bad}.Dockerfile` (`good` = distroless + non-root; `bad` = adds `COPY --from=busybox /bin/sh /bin/sh` and `USER root`) and asserts `scripts/image-policy.sh` exits 0 for `good` and non-zero for `bad` with stderr containing `policy: shell present` and `policy: ... runs as root`
- [X] T043 [P] [US3] Test `tests/unit/test_pin_images.py`: `scripts/pin-images.sh --check` exits non-zero on a temp compose file containing an unpinned `image: postgres:17` and 0 when every image carries `@sha256:`

### Implementation for User Story 3

- [X] T044 [P] [US3] Write `scripts/image-policy.sh <image>` implementing rules 1–7 from `contracts/image-policy.md` exactly: shell exec attempt; `docker create` + `docker export | tar -t` grep for shells and package managers; `docker inspect .Config.User` non-root; run with `--read-only --tmpfs /tmp` and wait for healthy; `docker buildx imagetools inspect` contains `linux/arm64`; `docker inspect .Config.Entrypoint` is a JSON array; every `FROM` in the corresponding Dockerfile has `@sha256:`; each failure prints `policy: <rule text> in <image>: <detail>` to stderr and the script exits 1 after checking all rules
- [X] T045 [US3] Write `.github/workflows/ci.yml` with `runs-on: ubuntu-24.04-arm` everywhere: job `lint` (`uv sync --all-packages`, `ruff format --check`, `ruff check`, `mypy`; web `npm ci`, `eslint`, `tsc --noEmit`); job `test` (`pytest -m "not integration"`, `vitest run`); job `build` (matrix over the six first-party images + `otel.Dockerfile`, `docker buildx build --platform linux/arm64 --load`, tag `ghcr.io/mcteer/coire/<name>:${{ github.sha }}`, `docker save` as artefact); job `image-policy` (needs build; matrix over the six first-party images **plus `coire-otel`** per contracts/image-policy.md; runs T044, `continue-on-error: false`); job `scan` (needs build; `aquasecurity/trivy-action` with `severity: CRITICAL`, `exit-code: 1`, `ignore-unfixed: false`, `format: table` so a failure names the CVE and package in the job log — this satisfies US3 scenario 3); job `sbom` (needs build; `anchore/sbom-action` SPDX JSON, upload artefact per image); job `integration` (needs build; loads artefacts, sets `COIRE_SECRET_*` from `openssl rand`, runs `COIRE_INTEGRATION=1 pytest tests/integration`); job `pin-check` (`scripts/pin-images.sh --check`)
- [X] T046 [US3] Add the push path to `ci.yml`: on `push` of a tag matching `v*`, jobs `build` push to `ghcr.io/mcteer/coire/<name>:<tag>` and `:<tag>@sha256` outputs are written to a job summary in the `images.lock` format, and the SBOM is attached as an OCI referrer (`oras attach`); on PRs nothing is pushed
- [X] T047 [P] [US3] Create `tests/fixtures/policy/good.Dockerfile` and `bad.Dockerfile` for T042, and a documented SC-008 procedure in `docs/runbooks/bootstrap.md` ("CI: proving the shell check") describing the throwaway-branch fixture from quickstart §4
- [ ] T048 [US3] Run the SC-008 fixture for real: branch `spike/sc-008-shell-fixture`, add the shell `COPY` to `apps/coire-api/docker/api.Dockerfile`, push, capture the failing `image-policy` log line into `docs/runbooks/bootstrap.md`, delete the branch

**Checkpoint**: CI green on `feat/000-bootstrap`; SC-004, SC-005, SC-007, SC-008 demonstrated.

---

## Phase 6: User Story 4 — Node agent on a Studio reports in (Priority: P2)

**Goal**: A launchd-managed agent on each Studio, installed only under `/opt/coire`, answers `/node/health` over the mesh with live CPU/GPU/memory/disk/thermal figures and its own resource use, registers with core at boot, survives a reboot unattended, and serves Wi-Fi requests only in explicit, counted fallback mode.

**Independent Test**: quickstart §5 — install on `coire-edge-a`; `curl http://coire-edge-a.mesh:9400/node/health` → 200 with `path: "mesh"` and budget fields within limits; node appears healthy in core's `/health`; after `sudo reboot` the endpoint answers within 2 min with no login; `uninstall.sh --dry-run` lists only `/opt/coire/**`, the plist, and one Keychain item; Wi-Fi without header → 403, with header → `path: "fallback"` + WARNING + counter; repeat on `coire-edge-b` (one-hop path).

### Tests for User Story 4

- [X] T049 [P] [US4] Contract test `apps/coire-node/tests/contract/test_node_health.py`: `/node/health` returns 401 without `Authorization: Bearer <node token>` and 401 with a wrong token (FR-013); with the right token on the mesh listener it validates against `NodeStatus` in `contracts/health-api.yaml`; on the egress listener returns 403 without `X-Coire-Path: fallback` and 200 with `path == "fallback"` with it; the fallback counter increments and a WARNING is logged (use `caplog`)
- [X] T050 [P] [US4] Unit test `apps/coire-node/tests/unit/test_metrics.py`: `collect()` returns all `NodeStatus` fields; `gpu_percent` is `None` when the IOAccelerator parser gets empty input and a float 0–100 when fed a captured `ioreg` fixture (`tests/fixtures/ioreg_ioaccelerator.txt`); `collection_budget_ok` flips to false when injected `agent_cpu_percent` exceeds the budget; sampling never blocks the event loop (runs in a thread)
- [X] T051 [P] [US4] Unit test `apps/coire-node/tests/unit/test_register.py`: registration payload matches `NodeRegistration`; token is read from the System keychain via an injected reader; on 401/403 the agent logs and retries with backoff (1 s → 60 s cap) rather than exiting; on network failure it keeps retrying (spec edge case "starts before network is up")
- [X] T052 [P] [US4] Contract test `apps/coire-api/tests/contract/test_register_node.py`: `POST /api/v1/nodes/register` → 200 + `Node` for a declared name with the right token; 401 wrong token; 403 undeclared name; 422 `mesh_address` outside `192.168.100.0/24`; upsert is idempotent on `name`; body validates against the contract schema
- [X] T053 [P] [US4] Unit test `apps/coire-node/tests/unit/test_install_footprint.py`: run `install.sh --dry-run` and `uninstall.sh --dry-run` against a temp prefix and assert the printed path set is exactly `{<prefix>/bin/uv, <prefix>/python/**, <prefix>/envs/<version>/**, <prefix>/envs/current, /Library/LaunchDaemons/com.coire.node.plist, keychain:coire-node-token}` and nothing under `/usr/local`, `/opt/homebrew`, or `$HOME` (FR-012a/b)

### Implementation for User Story 4

- [X] T054 [P] [US4] Implement `apps/coire-node/src/coire_node/metrics.py`: `psutil` for node CPU %, memory total/free, disk total/free of `/opt/coire`, and the agent's own `cpu_percent`/`rss`; `gpu.py` parsing `ioreg -r -c IOAccelerator -d 1` for `"Device Utilization %"` (return `None` on any parse failure); thermal state via `ioreg` `IOPMrootDomain` thermal level mapped to the `ThermalState` enum; a background sampler at `NODE_PROBE_INTERVAL_S` running in a thread, tracking a rolling window and setting `collection_budget_ok` against the configured budget (research R7)
- [X] T055 [P] [US4] Implement `apps/coire-node/src/coire_node/routes/health.py`: `GET /node/health` returning the latest `NodeStatus` sample with `path` set from the listener that received the request and `sampled_at` from the sample; `GET /ready`
- [X] T056 [US4] Implement `apps/coire-node/src/coire_node/agent.py`: resolve own mesh address by reading `/etc/hosts` for `<hostname>.mesh` and own egress address from the default-route interface; start two uvicorn servers on `:9400` — one bound to the mesh address, one bound to the egress address; a bearer-auth dependency on every route compares `Authorization: Bearer …` against the node's own token from the System keychain with `hmac.compare_digest` and returns 401 otherwise (FR-013); the egress listener additionally has middleware that returns 403 unless `X-Coire-Path: fallback` is present, incrementing OTel counter `coire_node_fallback_requests_total` and logging WARNING with the client address (research R9); OTLP export to `http://coire-core.mesh:4317` via the MeshClient fallback rules; `__main__.py` entry point
- [X] T057 [US4] Implement `apps/coire-node/src/coire_node/register.py`: at startup and every `NODE_PROBE_INTERVAL_S × 6`, POST `NodeRegistration` to `http://coire-core.mesh/api/v1/nodes/register` via `coire_core.net.MeshClient` (so fallback to `coire-core.local` applies); token from `security find-generic-password -w -s coire-node-token /Library/Keychains/System.keychain`; `gpu_cores` from `system_profiler SPDisplaysDataType` once at startup; infinite retry with capped backoff, never exit
- [X] T058 [US4] Implement the core-side prober in `apps/coire-api/src/coire_api/nodes_prober.py`: background task started in `create_app()` lifespan; every `NODE_PROBE_INTERVAL_S` calls `GET /node/health` on each registered node via `MeshClient`, sending `Authorization: Bearer <Settings.node_tokens[name]>`; on success sets `last_seen_at` and `reachability=healthy`; after 3 consecutive failures sets `unreachable`; feeds the `nodes[]` section of `/health` (data-model.md state transitions)
- [X] T059 [P] [US4] Write `deploy/launchd/com.coire.node.plist.template`: `Label com.coire.node`, `ProgramArguments [/opt/coire/envs/current/bin/python3, -m, coire_node]`, `UserName __USER__`, `RunAtLoad true`, `KeepAlive true`, `ThrottleInterval 10`, `EnvironmentVariables` with `COIRE_ROLE=studio`, `StandardOutPath`/`StandardErrorPath` under `/opt/coire/log/` (research R6)
- [X] T060 [US4] Write `apps/coire-node/install.sh` (runs on the Studio, no sudo except the plist step): requires `/opt/coire` to exist and be owned by the current user (prints the one-time `sudo mkdir/chown` otherwise); `UV_INSTALL_DIR=/opt/coire/bin` standalone uv install pinned to 0.12.7 (verify sha256); `UV_PYTHON_INSTALL_DIR=/opt/coire/python uv python install 3.13`; create `/opt/coire/envs/<version>` with `uv venv --python 3.13` and `uv pip install --no-deps` from the wheel built by `uv build` on core and copied over (`--wheel <path>`); flip `/opt/coire/envs/current` symlink; render the plist to `/Library/LaunchDaemons/com.coire.node.plist` via `sudo` and `launchctl bootstrap system`; `scripts/apply-mesh-hosts.sh`; `--dry-run` prints every path it would create (research R5)
- [X] T061 [P] [US4] Write `apps/coire-node/uninstall.sh`: `launchctl bootout system/com.coire.node`, remove the plist, remove `/opt/coire/{bin,python,envs,log}` (leaves `/opt/coire` itself for the operator), optionally `--keychain` deletes the token; `--dry-run` lists exactly what would be removed
- [X] T062 [US4] Write `apps/coire-node/pyproject.toml` `[project.scripts]` and a `scripts/build-node-wheel.sh` on core that runs `uv build --package coire-node` (and `coire-core`) producing wheels under `dist/`, then `scp`s them to `<host>.mesh:/opt/coire/dist/` for `install.sh --wheel` — the only transfer path, over the mesh
- [ ] T063 [US4] Manual verification on the real cluster (Principle VII): execute quickstart §5 on `coire-edge-a` then `coire-edge-b` including the 401/200 auth check, reboot, footprint audit, fallback tests, the `bridge0 down` partition test, and 200 authenticated probes measuring p50/p95 round-trip over the mesh (p95 MUST be ≤ 50 ms per plan Technical Context); record timings, p50/p95, `agent_cpu_percent`, `agent_rss_bytes`, and the exact `uninstall.sh --dry-run` output in `docs/runbooks/bootstrap.md` under "Node agent"

**Checkpoint**: SC-006 demonstrated on both Studios; FR-012a/b/c and FR-013a/b/c verified on real hardware.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Runbook, OTel verification, web SPA stub, pre-commit, and the full quickstart pass.

- [X] T064 [P] Implement the SPA stub in `apps/coire-web/src/App.tsx`: fetches `/health` and renders `status`, `version`, and the `services`/`nodes` tables with `healthy` badges (no auth, no routing yet); `apps/coire-web/src/App.test.tsx` renders against a mocked `/health` response
- [X] T065 [P] Add `apps/coire-node/src/coire_node/otel.py` and confirm end-to-end: node spans/metrics reach core's collector over the mesh (`docker compose logs otel-collector | grep -c ResourceSpans` > 0 after node probes), per quickstart §6
- [X] T066 [P] Add `scripts/coire-secrets-init.sh`: creates the three Keychain items on core with `openssl rand` values and prints the per-node token JSON for the operator to store on each Studio's System keychain (quickstart Prerequisites), refusing to overwrite an existing item without `--force`
- [X] T067 [P] Add `.github/pull_request_template.md` with the CONTRIBUTING §6 sections (what/why, spec link, constitution check, testing, observability, operational notes) and enable `pre-commit` in CI's `lint` job
- [X] T068 Complete `docs/runbooks/bootstrap.md`: Bring up (`coire-secrets-init.sh` once, `docker login ghcr.io -u mcteer --password-stdin <<< "$(gh auth token)"` once unless packages are public, `coire-up`; local development uses `compose.override.dev.yaml`, never `coire-up`), Bring down (`coire-down`, `--purge`), Restart one service (table from T041), Where to look (`docker compose ps/logs`, `/health` fields, node `/node/health`, collector logs), Roll back (edit `images.lock` to the previous digests, `coire-up`), Node agent (from T063), CI shell-check proof (from T048)
- [ ] T069 Run the complete `specs/000-bootstrap/quickstart.md` §1–§7 end to end on the real cluster from a clean state (`coire-down --purge` first), fix anything that fails, and record the SC-001 bring-up time and SC-003 web restart time in the runbook
- [X] T070 Final constitution re-check against `specs/000-bootstrap/plan.md`: confirm ADR-0001, ADR-0002 and ADR-0003 still describe exactly what shipped (no user-facing auth; bearer token on `/node/health` only; loopback-only publish; no raw IPs outside `deploy/cluster/hosts`; OTLP export wired, panel/alert owed to 009), and update the plan's "Post-design re-check" paragraph if anything diverged

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies; T002–T006 parallel after T001
- **Foundational (Phase 2)**: depends on Phase 1; **blocks all stories**. T007–T012, T015, T017–T019 parallel; T013 needs T009; T014 needs T009, T012; T016 needs T001
- **US1 (Phase 3)**: depends on Phase 2. This is the MVP and the only story that creates the compose project
- **US2 (Phase 4)**: depends on US1 (it restarts what US1 brought up). T035 can be written as soon as T031 exists
- **US3 (Phase 5)**: depends on Phase 2 and on the Dockerfiles from US1 (T016, T026, T028–T030) — but not on a running stack; can proceed in parallel with US2
- **US4 (Phase 6)**: depends on Phase 2 and on T024/T031 (registration endpoint and a running core); node-side tasks T049–T051, T053–T057, T059–T062 need no running stack and can proceed in parallel with US2/US3
- **Polish (Phase 7)**: depends on all four stories

### User Story Dependencies

- **US1 (P1)**: after Phase 2 — no story dependencies
- **US2 (P1)**: after US1 — restarts and topology test operate on US1's compose project
- **US3 (P2)**: after Phase 2 + US1's Dockerfiles — independent of US2 and US4
- **US4 (P2)**: after Phase 2 + T024 — independent of US2 and US3; verification (T063) needs the US1 stack running

### Within Each User Story

- Tests first, failing, then implementation
- `coire-core` models → api routes → Dockerfiles → compose → scripts
- Verification tasks (T041, T048, T063, T069) last, on real hardware where required

### Parallel Opportunities

- Phase 1: T002, T003, T004, T005, T006 together after T001
- Phase 2: T007, T008, T009, T010, T012, T015, T017, T018, T019 together; then T011, T013, T014, T016
- US1: T020, T021, T022 together; T027, T028, T029, T030, T033 together after T026
- US2: T035, T036, T037 together
- US3: T042, T043, T044, T047 together
- US4: T049–T053 together; then T054, T055, T059, T061 together; T056–T058, T060, T062 sequential
- After Phase 2: US3 and US4 node-side work can run alongside US1/US2 by different people

---

## Parallel Example: User Story 1

```bash
# Tests first, all three together (they must fail until T023–T032 land):
Task: "Contract test /ready and /health in apps/coire-api/tests/contract/test_health_api.py"
Task: "Contract test OpenAPI matches contracts/health-api.yaml in apps/coire-api/tests/contract/test_openapi_matches.py"
Task: "Integration test bring-up in tests/integration/test_bringup.py"

# After T026 (api/mcp/scheduler/migrate Dockerfiles), the remaining images and files together:
Task: "nginx.conf in apps/coire-web/nginx/nginx.conf"
Task: "Web Dockerfile in apps/coire-web/Dockerfile"
Task: "Agent Dockerfile + stub in apps/coire-agent/"
Task: "Collector config + derived image in deploy/compose/otel-collector.yaml, otel.Dockerfile"
Task: "coire-down in deploy/compose/coire-down"
```

## Parallel Example: User Story 4

```bash
# All five tests together:
Task: "Node health contract test in apps/coire-node/tests/contract/test_node_health.py"
Task: "Metrics unit test in apps/coire-node/tests/unit/test_metrics.py"
Task: "Register unit test in apps/coire-node/tests/unit/test_register.py"
Task: "Register endpoint contract test in apps/coire-api/tests/contract/test_register_node.py"
Task: "Install footprint test in apps/coire-node/tests/unit/test_install_footprint.py"

# Then metrics, health route, plist, uninstall together:
Task: "metrics.py + gpu.py in apps/coire-node/src/coire_node/"
Task: "routes/health.py in apps/coire-node/src/coire_node/routes/health.py"
Task: "plist template in deploy/launchd/com.coire.node.plist.template"
Task: "uninstall.sh in apps/coire-node/uninstall.sh"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 (T001–T006) → Phase 2 (T007–T019)
2. Phase 3 (T020–T034): the compose project comes up on core from one command
3. **STOP and VALIDATE**: quickstart §1 including the missing-secret negative case
4. Commit; open the PR draft — this alone proves Principle II-a holds

### Incremental Delivery

1. + US2 (T035–T041): restart isolation and the topology test → every later feature inherits the guard
2. + US3 (T042–T048): CI on arm64 with policy, scan, SBOM → branch protection can require it
3. + US4 (T049–T063): node agents on both Studios → feature 001 has live nodes to attach engines to
4. Phase 7 (T064–T070): runbook, full quickstart pass, constitution re-check → PR ready for review

### Parallel Team Strategy

- One person: US1 → US2, while a second does US3 (CI) and a third does US4 node-side (T049–T062); US4's T063 waits for the US1 stack

---

## Notes

- Every Dockerfile `FROM` and every compose `image:` is digest-pinned via `images.lock`; `scripts/pin-images.sh --check` runs in CI (T045) and in `test_topology.py` (T035)
- No task writes a secret to disk; `coire-up --secrets-from-env` exists only so CI can inject random secrets (T032, T045)
- Nothing lands on a Studio outside `/opt/coire`, the plist, and one Keychain item; T053 enforces it
- No auth on any route and no `cloudflared` — by decision (ADR-0001); do not add either "for later"
- Third-party images (`postgres:17`, socket proxy, collector) keep their upstream shells; rules 1–3 apply to first-party images only (spec FR-004 as amended; research R3)
- Manual-verification tasks (T041, T048, T063, T069) are real acceptance work, not optional — Principle VII requires cluster verification for anything CI cannot exercise
