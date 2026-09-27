# Implementation Plan: Coire-Ops with Confirmed Mutations

**Branch**: `feat/012-coire-ops-confirmed-mutations` | **Date**: 2026-09-01 | **Spec**: `specs/012-coire-ops-confirmed-mutations/spec.md`

## Summary

Turn the existing separate ops distribution and read-only Ask Coire endpoint into a long-lived,
core-only `coire-ops` service. The service calls the pinned Studio-B admin model through the gateway,
uses a narrow internal credential to read facts and submit only typed allowlisted proposals, and has
no route that can approve or execute them. `coire-api` persists the conversation and proposal,
mints a hashed short-lived exact-action token, and atomically consumes it only for a human admin
before dispatching an existing reversible mutation service. A volatile ops session generation makes
all pending proposals from an earlier container lifetime unapprovable. If the admin model is not
ready, the existing deterministic snapshot responder answers read-only questions without a model.

## Technical Context

**Language/Version**: Python 3.13; TypeScript 5 / React 19
**Primary Dependencies**: existing FastAPI, Pydantic v2, SQLAlchemy async, Alembic, httpx, Pydantic AI, Argon2, OTel
**Storage**: PostgreSQL conversations, messages, proposals, confirmation-token hashes, session generations, and audit rows; no durable state in the ops container
**Testing**: pytest unit/contract tests, composed integration with the fake tiny model, Vitest/Testing Library, image policy/scan/SBOM checks
**Target Platform**: Linux/arm64 distroless `coire-ops` container on core; React SPA served by nginx; models and Metal remain on Studios
**Project Type**: shared contracts, authenticated control-plane API, isolated internal service, and admin web flow
**Performance Goals**: proposal/decline/confirm API p95 under 250 ms excluding model and mutation workflow time; degraded snapshot answer under 500 ms; confirmation race yields exactly one execution
**Constraints**: no model, engine, Docker socket, filesystem, shell, git, Studio endpoint, or general admin credential in `coire-ops`; token TTL at most 5 minutes by default; fixed reversible action registry
**Scale/Scope**: one ops container, a small number of admins, up to 100 pending proposals, five initial reversible action kinds

## Constitution Check

- **I**: `coire-ops` calls inference only through authenticated `/v1`; existing API/scheduler/node services retain all bare-engine lifecycle authority.
- **II**: the sole core harness is the explicitly permitted ops service. Its configured model is pinned on Studio B and no core code can load weights or invoke Metal.
- **II-a**: `coire-ops` remains a distinct distroless image and one-process container, non-root, read-only, capability-free, resource-limited, health-checked, and connected only to `coire-internal` and telemetry.
- **III**: conversation, message, proposal, resolved action, confirmation, decline, internal service, and response shapes are strict Pydantic models in `coire-core`; OpenAPI and TypeScript are generated.
- **IV**: the ops service receives propose/read scope only. A human admin must redeem a random Argon2-hashed, expiring, single-use token under a row lock; proposal, refusal, decline, and execution are audited without token or prompt leakage.
- **V**: load actions accept registry model/variant UUIDs resolved by the API. The ops service cannot acquire models or pass paths/repositories to engines.
- **VI**: ask/propose/confirm/decline/execute/degraded paths emit spans, bounded metrics, structured identifiers, a dashboard panel, and alerts for repeated refusals or degraded duration.
- **VII**: contract, concurrency, restart invalidation, degraded-mode, browser, image, and composed tiny-model tests are required before completion.

No constitutional exception is required. The service credential can submit a proposal but is
structurally unable to confirm it; confirmation authority remains exclusively with an authenticated
human admin.

## Project Structure

```text
packages/coire-core/src/coire_core/models/ops.py
apps/coire-api/src/coire_api/{ops,ops_actions,ops_tokens}.py
apps/coire-api/src/coire_api/routes/{admin_ops,internal_ops}.py
apps/coire-api/alembic/versions/*_ops_confirmations.py
apps/coire-agent/ops/coire_ops/{app,service,admin_client,model}.py
apps/coire-agent/ops.Dockerfile
apps/coire-web/src/{api,components,pages/admin}/
deploy/compose/{compose.yaml,README.md}
deploy/observability/{grafana/provisioning/dashboards,prometheus/rules}/
docs/runbooks/coire-ops.md
tests/integration/test_ops_confirmations.py
```

**Structure Decision**: extend the existing ops-only distribution, control-plane service boundary,
shared schemas, and Ask Coire UI. Do not put ops code in the user agent image, scheduler, node agent,
or gateway `/v1` adapter.

## Design Phases

1. Define strict ops action/conversation/proposal/session/confirmation contracts and persisted state.
2. Implement ops service authentication, volatile session registration/heartbeat, proposal creation,
   Argon2 token minting, atomic human confirmation, decline, stale-state checks, and audit.
3. Implement the long-lived model-backed ops service with only read/propose tools and deterministic
   no-model degraded responses.
4. Dispatch confirmed actions through the existing instance/run/model services and fixed action
   registry; add the exact proposal/approval UI.
5. Add compose hardening, secrets, telemetry, dashboard/alerts, runbook, concurrency/restart tests,
   composed tiny-model validation, and image gates.

## Post-Design Constitution Re-check

Passed. The contracts keep action authority in `coire-api`; the container session invalidates prior
pending proposals on restart; exact-action digests and row locking prevent redirection/replay; the
degraded path loads no model; and core gains neither an engine nor a user harness.

## Complexity Tracking

No constitutional violation or exceptional complexity is introduced.
