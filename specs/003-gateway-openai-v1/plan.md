# Implementation Plan: Gateway and OpenAI-Compatible /v1

**Branch**: `feat/003-gateway-openai-v1` | **Date**: 2026-08-31 | **Spec**: `specs/003-gateway-openai-v1/spec.md`

**Input**: Feature specification from `/specs/003-gateway-openai-v1/spec.md`

## Summary

Add typed OpenAI Chat Completions, model-listing, and Anthropic Messages surfaces to `coire-api`.
The gateway resolves opaque registry UUIDs before engine contact, asks the existing registry/node
control path to load at most one engine per model, streams engine responses without buffering,
records durable usage for success/failure/disconnect, and leaves placement, real user auth, and
multi-instance routing behind their existing feature seams.

## Technical Context

**Language/Version**: Python 3.13

**Primary Dependencies**: FastAPI, Pydantic v2, httpx, SQLAlchemy 2 async, PostgreSQL 17,
OpenTelemetry; official OpenAI and Anthropic Python SDKs as development-only compatibility clients

**Storage**: PostgreSQL additive `usage_records` migration; existing model, copy, node, and engine rows

**Testing**: pytest unit/contract, generated OpenAPI freshness, official-SDK integration against the
existing tiny fake/real engine harness

**Target Platform**: `coire-api` Linux/arm64 container on core; bare `mlx_lm.server` on macOS Studios

**Project Type**: Async API gateway within the existing monorepo

**Performance Goals**: ≤20 ms p95 gateway overhead excluding queue/load/model time; ≤1.5 s p95
first token for a loaded tiny model and ≤4k-token prompts

**Constraints**: Engine ports remain reachable only through the gateway/node control path; no
caller model string reaches an engine; streams are cancellation-aware; wait ceiling and per-engine
in-flight caps are configured; `/v1` wire fields remain compatible and Coire additions use `coire_`

**Scale/Scope**: Three-node lab, two Studios, one active engine per model until feature 005, bounded
single-process gateway concurrency

## Constitution Check

*GATE: Passed before research and re-checked after design.*

- **I — Bare engines**: PASS. The gateway proxies the existing bare MLX server and adds no wrapper.
- **II — Core hosts no models/harness**: PASS. Core performs routing/accounting only.
- **II-a — One service/container**: PASS. The surface is added to the existing API role; no second
  process or shared production image is introduced.
- **III — Contracts first**: PASS. All OpenAI, Anthropic, usage, and error shapes originate in
  `coire-core`; OpenAPI and contract tests change together.
- **IV — Zero implicit trust**: PASS with the existing ADR-0004 transition seam. Every route depends
  on `CurrentPrincipal`; feature 007 replaces identity resolution without changing gateway logic.
- **V — Models are data**: PASS. Only a registry UUID resolves; paths and slugs are database-derived.
- **VI — Observable**: PASS. Gateway spans, latency/in-flight/usage metrics, structured identifiers,
  dashboard hooks, and an overload/engine-failure alert are required.
- **VII — Spec/test gated**: PASS. Contract tests precede routes and integration uses the tiny model.

Post-design re-check: PASS. The design adds one reversible migration and no new production service,
network, secret, capability, or engine exposure.

## Project Structure

### Documentation (this feature)

```text
specs/003-gateway-openai-v1/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/openapi.yaml
└── tasks.md
```

### Source Code (repository root)

```text
packages/coire-core/src/coire_core/models/
└── gateway.py

apps/coire-api/
├── alembic/versions/0003_gateway_usage.py
├── src/coire_api/
│   ├── db.py
│   ├── gateway/
│   └── routes/v1.py
└── tests/
    ├── contract/test_gateway_v1.py
    └── unit/test_gateway_*.py

tests/integration/
└── test_gateway.py

deploy/observability/
└── gateway panels and alert rules
```

**Structure Decision**: Extend the existing core schema and API packages. Routing remains in
`coire-api`; node/engine lifecycle remains behind `NodeClient`, preserving Principles I and II.

## Complexity Tracking

No constitutional violations require exceptions.
