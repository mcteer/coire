# AGENTS.md — guidance for coding agents working in Coire

Coire is the control plane for a three-node Apple Silicon AI lab: a Mac Mini ("core") that orchestrates, and two Mac Studios that run models. Read this file first; then `.specify/memory/constitution.md` (binding principles), `docs/ARCHITECTURE.md` (the design), and `docs/ROADMAP.md` (feature order). Every feature is developed spec-first with GitHub Spec Kit; the spec for your task lives in `specs/NNN-<slug>/`.

## Non-negotiables (from the constitution — cite the principle in your PR)

- **I. Bare engines.** Drive `mlx_lm.server`, `mlx.launch`, `mlx_lm.lora`, `mlx_lm.convert`, `mflux` directly. Never add Ollama, LM Studio, exo, LocalAI, or any inference wrapper, and never expose an engine port beyond coire-node and the gateway.
- **II. Core hosts no models and runs no user harness.** Only `coire-ops` runs on core. User-facing harnesses (`coding`, `general`, `image`) run as containers on the Studios via coire-node. Do not add code paths that load weights or start Metal work on core.
- **II-a. One service, one container, bare image.** New services get their own distroless/Chainguard image, non-root, read-only rootfs, dropped capabilities, per-concern network, healthcheck. No shell or debug tools in production images.
- **III. Contracts first.** Every wire shape (REST, MCP, node agent, job payload, harness result) is a Pydantic model in `packages/coire-core`. OpenAPI is generated. `/v1` stays OpenAI-compatible; Coire fields are additive and prefixed `coire_`.
- **IV. Zero implicit trust.** Every route is authenticated and scoped; admin mutations and entitlement changes write an audit row; agent runs get short-lived run tokens and a kill switch; secrets come from Keychain-sourced compose secrets, never the repo or an image.
- **V. Models are data; only admins acquire them.** Model ids come from the registry; never pass a caller-supplied model/adapter string to an engine; never trigger a Hugging Face pull outside the admin acquisition pipeline; a model must be `verified` before write-capable agent tasks may use it.
- **VI. Observable or it doesn't ship.** New code paths emit OTel spans, metrics, and structured logs; a feature includes its dashboard panel and at least one alert rule.
- **VII. Spec-driven, test-gated.** No implementation without a spec and plan; contract tests for every API surface; integration tests run against a tiny model on one Mac.

## Repository map

```
packages/coire-core/   shared Pydantic schemas, enums, Conversation type, client SDK   (change here first)
apps/coire-api/        FastAPI gateway + control-plane API (+ /v1/messages adapter)
apps/coire-mcp/        MCP server: exactly research / plan / apply
apps/coire-scheduler/  placement ledger, instances, DBOS workflows, jobs, auto-unload
apps/coire-ops/        the ops harness (only harness allowed on core)
apps/coire-node/       Studio node agent: engines, image worker, run containers, probes
apps/coire-agent/      Pydantic AI user harnesses + Dockerfile (runs on Studios)
apps/coire-web/        React + TypeScript SPA (chat + admin), served by nginx container
deploy/compose/        core control plane (one container per service)
deploy/launchd/        coire-node plists for the Studios
deploy/cluster/        JACCL hostfile template, distributed_config script
recipes/               versioned TrainingSpec / ImageSpec presets
specs/                 Spec Kit feature directories (spec.md, plan.md, tasks.md)
docs/                  ARCHITECTURE, ROADMAP, runbooks/, adr/
.specify/memory/       constitution.md (binding principles)
```

## Setup and everyday commands

```bash
uv sync --all-packages                 # Python 3.13 workspace; never pip install
uv run ruff format && uv run ruff check --fix
uv run mypy                            # strict; new modules must pass
uv run pytest -q                       # unit + contract tests (no engines needed)
uv run pytest -q -m integration        # needs a tiny model in $COIRE_TEST_MODEL (≤1 GB)
pnpm -C apps/coire-web install && pnpm -C apps/coire-web test && pnpm -C apps/coire-web lint
docker compose -f deploy/compose/compose.yaml up -d   # control plane on core (or a dev Mac)
uv run coire --help                    # CLI mirrors the admin API
```

Pre-commit runs ruff, mypy, eslint/prettier, and the OpenAPI freshness check (`uv run coire-api export-openapi --check`). A PR that changes a schema must regenerate `apps/coire-web/src/api/schema.d.ts` and update contract tests in the same commit.

## How to work on a task

1. Find or create the spec: `specs/NNN-<slug>/spec.md` via `/speckit.specify`, then `/speckit.clarify`, `/speckit.plan` (include the Constitution Check), `/speckit.tasks`. If a task has no spec, stop and ask for one — bug fixes are the exception and use an issue instead (see CONTRIBUTING.md).
2. Work on the branch named in the spec (`feat/NNN-<slug>` or `fix/<issue>-<slug>`). Never commit to `main`.
3. Change `coire-core` schemas first, then services, then the web client, keeping each commit green.
4. Add or update tests alongside code: contract tests for routes, unit tests for scheduler/ledger logic, an integration test against the tiny model when behaviour touches an engine. Do not mark work complete with failing or skipped tests.
5. Add observability with the code: span names `coire.<service>.<operation>`, metrics prefixed `coire_`, structured log fields `run_id`, `job_id`, `instance_id`, `model_id`, `user_id` where applicable.
6. Update `docs/runbooks/` for anything operational you add (how to see it, kill it, roll it back) and `docs/adr/` for any decision that deviates from the architecture.
7. Open a PR using the template; link the spec; list which constitution principles the change touches and how it complies.

## Conventions

- Python: 3.13, type-annotated everywhere, `async` for I/O, Pydantic v2 models (`model_config = ConfigDict(extra="forbid")` on wire types), `httpx` for HTTP, SQLAlchemy 2 async + Alembic migrations (one migration per PR, reversible), DBOS for durable workflows in `coire-scheduler` only.
- Pydantic AI: one `Agent` per profile in `apps/coire-agent/profiles/`; tools small and flat (≤10 per profile, no nested unions in tool schemas); model choice comes from the capability profile and tag preference, never a hard-coded model name; use `ModelRetry` for validation feedback; never disable the harness eval gate.
- Engines: only coire-node spawns processes. It does so from `apps/coire-node/engines/*.py` with explicit argv (no shell strings), the versioned env under `/opt/coire/envs/<version>`, and always records pid, port, and memory reservation before returning.
- Web: React function components + hooks, TypeScript strict, generated API types only (no hand-written response shapes), SSE via the shared `useEventStream` hook, no direct `fetch` outside `src/api/`.
- Naming: registry ids are slugs (`qwen3-coder-32b@4bit-g64`), instances and jobs are ULIDs, adapters are `model@adapter`.
- Errors: raise `CoireError` subclasses from `coire-core`; the gateway maps them to RFC 9457 problem details. Never leak engine stack traces to clients.
- Config: environment variables documented in `deploy/compose/README.md`; no config read from anywhere else at runtime.

## Things agents must not do

- Do not add dependencies without a line in the PR explaining why and confirming the licence; pin exact versions in the lockfile.
- Do not widen a CORS, network, capability, or firewall setting to make something work; ask.
- Do not write to `main`, force-push shared branches, or rewrite history on a branch someone else has reviewed.
- Do not commit secrets, `.env` files, model weights, datasets, or generated images. `.gitignore` covers `/models`, `/data`, `/blobs`; keep it that way.
- Do not disable or loosen tests, type checks, image scans, or the OpenAPI freshness check to get green.
- Do not implement admin, explicit-content, or Hugging Face acquisition behaviour outside the admin API and audit path.
- Do not run `mlx.launch`, engine processes, or `docker` against the real Studios from CI or from your own environment; integration tests use the tiny model locally.

## Definition of done for any change

Spec linked · constitution check passed · schema in `coire-core` · migration if needed · tests green (unit, contract, integration where applicable) · OTel/metrics/alert added · runbook updated · OpenAPI and TS types regenerated · images build and scan clean · PR template complete.
