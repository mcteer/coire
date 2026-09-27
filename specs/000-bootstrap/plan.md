# Implementation Plan: Bootstrap Control Plane Skeleton

**Branch**: `000-bootstrap` (git branch `feat/000-bootstrap` per CONTRIBUTING §2) | **Date**: 2026-08-29 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/000-bootstrap/spec.md`

## Summary

Stand up the skeleton every later feature builds on: a `uv` workspace with five packages; one
shell-less, non-root, digest-pinned image per control-plane service; a compose project on core
that brings eight containers up on five per-concern networks with health-gated dependencies and
Keychain-sourced secrets that never touch disk; a launchd-managed node agent on each Studio
reporting live CPU, GPU, memory and disk over the Thunderbolt mesh with an alerted Wi-Fi
fallback; and a CI pipeline on native arm64 runners that builds, scans, SBOMs and
policy-checks every image while running lint and tests. No inference, no auth, no public
exposure — those are deliberately deferred and recorded as such.

## Technical Context

**Language/Version**: Python 3.13 (pinned; `requires-python = "==3.13.*"`, provisioned by `uv`
in containers and on the Studios). TypeScript for the SPA, React + Vite, versions pinned in the
lockfile at implementation.

**Primary Dependencies**: FastAPI, Pydantic v2, pydantic-settings, uvicorn, SQLAlchemy 2
(async) + asyncpg, Alembic, OpenTelemetry SDK + OTLP exporter + FastAPI instrumentation,
httpx, psutil. Build: `uv 0.12.7`. Web: React, Vite, nginx (Chainguard). CI: GitHub Actions,
Trivy, Syft.

**Storage**: Postgres 17 (container on core, named volume). In this feature it holds only the
Alembic version table and the `nodes` table.

**Testing**: `pytest` + `pytest-asyncio` + `httpx` ASGI client for unit and contract tests;
compose bring-up with health probes as the integration test in CI; `vitest` for the SPA;
shell-based image-policy checks in CI. Node-agent-on-Studio behaviour is verified manually on
the real cluster (quickstart §5) — it cannot run in CI.

**Target Platform**: `linux/arm64` containers under OrbStack on macOS 26.6 (core); native
macOS 26.6 launchd services on the Studios. arm64 only — no amd64 build.

**Project Type**: `uv` workspace monorepo — web service (`coire-api` + stubs), node agent
(`coire-node`), SPA (`coire-web`), shared library (`coire-core`), and an agent image built but
not run (`coire-agent`).

**Performance Goals**: Clean bring-up to all-healthy ≤ 3 min (SC-001). `coire-web` restart
< 5 s (SC-003). Node health probe latency is measured and recorded over the mesh (there is no
standalone sub-50 ms gate). Node agent steady state ≤ 2 % of
one core and ≤ 150 MB RSS, self-reported (FR-012c).

**Constraints**: First-party images shell-less, non-root, read-only rootfs, `cap_drop: [ALL]`,
digest-pinned (R1–R3). No secret file on host disk (R4). Studios receive nothing outside
`/opt/coire` plus one plist and one Keychain item (R5). No auth, no tunnel (R12). Platform
traffic prefers the mesh; egress fallback is explicit and counted (R9).

**Scale/Scope**: 3 hosts. 8 containers on core (`coire-web`, `coire-api`, `coire-mcp` stub,
`coire-scheduler` stub, `coire-migrate` one-shot, `postgres`, `docker-socket-proxy`,
`otel-collector`). 2 node agents. 5 networks. 6 first-party images (the five above plus
`coire-agent`).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.* — **Evaluated twice;
both passes below.**

| Principle / constraint | Verdict | Evidence |
|---|---|---|
| **I. Bare engines, owned lifecycle** | PASS (n/a) | No engine, model or wrapper in scope. `coire-node` spawns nothing (spec Assumptions). |
| **II. Control node disposable, workers sacred** | PASS | All stateful and orchestrating services in one compose project on core. Studios get the node agent only — no database, no web tier. `coire-agent` is built (FR-017) and never started on core. Nothing on a Studio is a source of truth. |
| **II-a. One service, one container, bare image** | PASS | One image and one process per service (FR-003); distroless runtime with pinned 3.13 proven on core (R1); non-root uid 65532, RO rootfs, `cap_drop: [ALL]`, `no-new-privileges`, healthcheck on every long-lived service, per-concern networks, Trivy + Syft in CI (R10). Third-party image scoping recorded in R3 and amended into FR-004. |
| **III. Contracts first, typed end to end** | PASS | `Settings`, `HealthResponse`, `ReadyResponse`, `NodeStatus`, `NodeRegistration` are Pydantic models in `coire-core` and the only wire shapes (FR-002). OpenAPI is generated from FastAPI, never hand-written; `contracts/health-api.yaml` is the reviewed shape the generated document must match. `/v1` is out of scope. |
| **IV. Public by design, zero implicit trust** | **EXCEPTION — ADR-0001** | No authentication on any route; no Cloudflare tunnel; static per-node token. All three are user decisions (2026-08-29) to defer until the platform is ready for people. Time-boxed: auth and edge → feature 007; issued node tokens → feature 005. The app is built so auth wires in without restructuring (R12). |
| **V. Models are data** | PASS (n/a) | No models in scope. |
| **VI. Observable or it doesn't ship** | **EXCEPTION — ADR-0003** | Traces and metrics export via OTLP to the collector (FR-014, R13). No dashboard panel or alert rule can exist because the backends are feature 009; 009's acceptance includes the bootstrap-health panel and the node-unreachable / fallback-counter alerts, and must record ADR-0003 as closed. |
| **VII. Spec-driven, test-gated, incremental** | PASS | spec → plan → tasks → implement on `feat/000-bootstrap`. Contract tests for `/health` and `/ready`; compose bring-up as the CI integration test. The "tiny model" clause does not apply (no model); manual verification on the real cluster is documented in quickstart §5. Smallest change: stubs for `mcp` and `scheduler` serve `/ready` only. |
| Tech: Python 3.13 / uv / FastAPI / Pydantic v2 / SQLAlchemy 2 / Alembic / Postgres 17 | PASS | R1, R3, R5. DBOS and Pydantic AI are not yet needed (features 002 and 010). |
| Tech: `/opt/coire/envs` with symlink flip | PASS | R5. Rollback mechanics themselves are feature 019. |
| Tech: compose only from `deploy/compose/`, CI-built tag-pinned images, socket proxy allowlist | PASS | Images pinned by digest; `docker-socket-proxy` reachable only from `coire-scheduler` on `coire-docker` (FR-007). |
| Tech: nginx sole ingress, no Node runtime in prod | PASS | R2; Vite build output only. |
| Tech: Observability stack | PASS (partial by design) | Collector only; rest is 009. |
| Tech: hosts by DNS names, never raw IPs in config | **EXCEPTION — ADR-0002** | The mesh is unrouted and has no UniFi DNS; mDNS measured non-deterministic (R8). A managed `/etc/hosts` block generated from `deploy/cluster/hosts` is the mesh's name service. No service config contains an IP. |
| Tech: forbidden — hand-edited prod config on nodes | PASS | Everything on a Studio is applied by `deploy/` scripts (R5, R8). |
| Tech: forbidden — long-lived static tokens for agents | Covered by ADR-0001 | The node agent is not an agent harness, but its static token is the same class of risk and is replaced in 005. |
| Quality: documentation → `docs/runbooks/` | PASS (task) | `docs/runbooks/bootstrap.md`: bring-up, bring-down, restart one service, where to look, roll back by re-pinning a tag. |
| Quality: latency, ledger drift, upgrades, safety | n/a | No gateway, ledger, upgrade or image generation in scope. |

**Post-design re-check (after Phase 1)**: unchanged. The data model adds no state on a Studio;
the contracts add no authenticated surface; the compose topology in `contracts/compose-topology.md`
keeps `coire-web` off `coire-db` and `coire-ops`/`cloudflared` absent. Both exceptions remain
exactly as scoped in their ADRs.

## Project Structure

### Documentation (this feature)

```text
specs/000-bootstrap/
├── plan.md              # This file
├── research.md          # Phase 0 — R1–R13, all resolved by measurement
├── data-model.md        # Phase 1 — Node, ServiceHealth, ImageTag, Settings
├── quickstart.md        # Phase 1 — validation scenarios mapped to SC-001…SC-008
├── contracts/
│   ├── health-api.yaml         # OpenAPI 3.1: /health, /ready, node /health, /register
│   ├── compose-topology.md     # services × networks × images × hardening matrix
│   └── image-policy.md         # the CI-enforced first-party image contract
└── tasks.md             # Phase 2 — /speckit-tasks (not created here)

docs/adr/
├── 0001-defer-auth-and-edge-until-external-traffic.md
├── 0002-mesh-name-resolution-via-managed-hosts-file.md
└── 0003-defer-dashboard-panel-and-alert-to-observability-stack.md
```

### Source Code (repository root)

```text
pyproject.toml                 # uv workspace root; requires-python ==3.13.*
uv.lock                        # committed
.python-version                # 3.13

packages/coire-core/
├── pyproject.toml
├── src/coire_core/
│   ├── settings.py            # pydantic-settings; secrets read from /run/secrets/*
│   ├── models/health.py       # HealthResponse, ReadyResponse, ServiceHealth
│   ├── models/node.py         # NodeStatus, NodeRegistration, NodeMetrics
│   └── net.py                 # mesh-first client: <host>.mesh then <host>.local + header
└── tests/

apps/coire-api/
├── pyproject.toml
├── src/coire_api/
│   ├── app.py                 # FastAPI factory; OTel instrumentation
│   ├── auth.py                # require_principal(): anonymous now; 007 replaces body
│   ├── routes/health.py       # /health (aggregate), /ready (liveness)
│   ├── routes/nodes.py        # POST /api/v1/nodes/register (static token)
│   ├── db.py                  # async engine from Settings
│   └── alembic/               # env.py + versions/0001_nodes.py
├── src/coire_mcp/main.py      # stub: /ready only (real MCP is feature 013)
├── src/coire_scheduler/main.py# stub: /ready only (real scheduler is feature 004)
├── docker/
│   ├── api.Dockerfile
│   ├── mcp.Dockerfile
│   ├── scheduler.Dockerfile
│   └── migrate.Dockerfile     # one-shot: alembic upgrade head; exit 0
└── tests/{unit,contract}/

apps/coire-node/
├── pyproject.toml
├── src/coire_node/
│   ├── agent.py               # two listeners (mesh, egress+fallback header)
│   ├── metrics.py             # psutil + IOAccelerator; self-budget reporting
│   ├── routes/health.py
│   └── register.py            # registers to coire-core.mesh with System-keychain token
├── install.sh                 # /opt/coire only; enumerable; renders deploy/launchd template
├── uninstall.sh               # --dry-run lists exactly what was installed
└── tests/{unit,contract}/

apps/coire-agent/
├── Dockerfile                 # built in CI, never started on core
└── src/coire_agent/__main__.py# prints version and exits (harness is feature 010)

apps/coire-web/
├── package.json, vite.config.ts, tsconfig.json
├── src/                       # SPA stub: renders /health
├── nginx/nginx.conf           # sole ingress; proxy_buffering off on /v1 /mcp /api
├── healthcheck/main.go        # static probe binary (R2)
└── Dockerfile

deploy/
├── compose/
│   ├── compose.yaml           # 8 services, 5 networks, images pinned from images.lock
│   ├── compose.override.dev.yaml  # build: blocks for local development on core only
│   ├── images.lock            # name → ref@sha256 for every image (CI-pushed, ghcr.io)
│   ├── otel-collector.yaml
│   ├── otel.Dockerfile        # upstream collector + static probe binary (R2)
│   ├── coire-up               # Keychain → env → compose up (R4); flock-guarded
│   └── coire-down
├── cluster/
│   ├── hosts                  # <host>.mesh → 192.168.100.x (ADR-0002)
│   └── nodes.yaml             # declared inventory: names + roles + gpu_cores, no IPs
└── launchd/
    └── com.coire.node.plist.template   # rendered by apps/coire-node/install.sh

scripts/
├── apply-mesh-hosts.sh        # managed /etc/hosts block (ADR-0002)
├── pin-images.sh              # resolve + verify digests (--check in CI)
├── image-policy.sh            # rules 1–7 from contracts/image-policy.md
├── build-node-wheel.sh        # uv build → scp over the mesh
└── coire-secrets-init.sh      # create the three Keychain items once

tests/
├── integration/               # test_bringup, test_restart_isolation, test_topology
├── unit/                      # image-policy and pin-images script tests
└── fixtures/                  # policy/{good,bad}.Dockerfile, ioreg_ioaccelerator.txt

.github/workflows/ci.yml       # ubuntu-24.04-arm; build/scan/sbom/policy/lint/test
.github/pull_request_template.md
docs/runbooks/bootstrap.md
```

**Structure Decision**: The `uv` workspace layout the architecture specifies (§10), with the
four control-plane images that share the `coire-api` codebase built from separate Dockerfiles
under `apps/coire-api/docker/` so no image serves two roles (FR-003). `mcp` and `scheduler` are
stubs that answer `/ready` and nothing else — enough to prove independent restart (SC-002)
without pre-empting features 004 and 013.

## Complexity Tracking

> Filled because the Constitution Check records two exceptions and one deviation.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| **Principle IV** — no auth, no edge, static node token (ADR-0001) | User decision: nothing faces the internet until the platform is ready for people; building auth now would be against a surface with no users and no identity provider configured. The app declares the auth seam (`require_principal`) so 007 wires in without restructuring. | Shipping a placeholder auth that "always succeeds" — worse than none, because it looks like protection. Shipping real auth now — blocks 000 on Cloudflare Access configuration that does not exist yet. |
| **Tech constraint: no raw IPs in config** — managed `/etc/hosts` block for the mesh (ADR-0002) | The mesh is unrouted and never reaches the UDM, so UniFi DNS cannot name it; mDNS was measured to resolve to link-local addresses non-deterministically. | mDNS — non-deterministic, measured. A DNS container on core — the mesh must survive core being down (feature 020). Putting IPs in service config — exactly what the constraint forbids; the hosts file keeps them in one place. |
| **Principle VI** — no panel or alert in this feature (ADR-0003) | The metrics/logs/traces backends do not exist until 009. Export is wired (FR-014) so nothing is lost; 009's acceptance explicitly includes the bootstrap panel and node-down alert. | Pulling Grafana/Prometheus into 000 — expands the feature past "skeleton" and duplicates 009. Skipping export — would make 009 retrofit instrumentation into every service. |
