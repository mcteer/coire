# Implementation Plan: Admin Console

**Branch**: `feat/008-admin-console` | **Date**: 2026-09-01 | **Spec**: `specs/008-admin-console/spec.md`

## Summary

Build the shared Glass-design React shell and server-enforced admin console over typed Coire contracts. Add a reconciliable admin SSE feed, paginated inventory contracts, optimistic concurrency, and audited lifecycle/identity operations; consume them through a generated-type API layer and reconnecting hook. Controls for later features remain absent. Ask Coire is strictly read-only and degrades when its pinned remote model is unavailable.

## Technical Context

**Language/Version**: Python 3.13; TypeScript 5.7; React 18

**Primary Dependencies**: FastAPI, Pydantic v2, SQLAlchemy 2 async, OpenTelemetry; React, Vite, native Fetch streams

**Storage**: PostgreSQL 17 existing entities and event cursor; browser memory for view state

**Testing**: pytest unit/contract/integration; Vitest + Testing Library; Playwright/WebKit

**Target Platform**: coire-api and nginx-served SPA on core; desktop browsers, adaptive below 1200 px

**Project Type**: FastAPI backend plus React SPA

**Performance Goals**: live changes within 2 seconds; stops within 5 seconds; bounded collections

**Constraints**: no polling; generated wire types; server-side admin enforcement; exact-once key disclosure; no unshipped controls; no engine work on core; accessible UI; no new frontend dependency

**Scale/Scope**: three nodes, tens of operational resources, large audit history, seven admin tabs and shared shell

## Constitution Check

*GATE: Passed before research and re-checked after design.*

- **I — Bare engines**: only Coire lifecycle routes are invoked; no wrapper/direct engine exposure.
- **II — Core/worker boundary**: core serves API/SPA only; Ask Coire calls a Studio-hosted model and never runs one locally.
- **II-a — Containers**: existing separate API/web images remain minimal and independently deployable.
- **III — Contracts first**: new page, event, pagination and operation shapes start as strict Pydantic models; OpenAPI and TypeScript regenerate.
- **IV — Zero trust**: every admin route rechecks role; mutations audit current principal; no browser static secret.
- **V — Models as data**: acquisition and lifecycle resolve registry entities through existing admin pipelines.
- **VI — Observable**: console spans, metrics, structured logs, dashboard panel and alert are included.
- **VII — Spec/test gated**: contract, UI, integration, browser, image, scan and OpenAPI gates are explicit.

Post-design re-check: **Passed**. No exception or ADR is required.

## Project Structure

### Documentation (this feature)

```text
specs/008-admin-console/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/admin-console-api.md
└── tasks.md
```

### Source Code

```text
packages/coire-core/src/coire_core/models/console.py
packages/coire-core/tests/test_console_models.py
apps/coire-api/src/coire_api/routes/admin_console.py
apps/coire-api/src/coire_api/console/
apps/coire-api/tests/contract/test_admin_console.py
apps/coire-api/tests/unit/test_console_service.py
apps/coire-web/src/{api,hooks,components,pages/admin,styles}/
tests/integration/test_admin_console.py
deploy/observability/
docs/runbooks/admin-console.md
```

**Structure Decision**: Extend `coire-core` → `coire-api` → generated TypeScript → `coire-web` in that order. Use the History API for shallow routes rather than adding a router dependency.

## Complexity Tracking

No constitutional violations. One aggregate stream is justified by FR-017: it atomically reconciles independently changing resources without one browser connection per row.
