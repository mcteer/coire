# Implementation Plan: Auth, Users, API Keys, and Audit

**Branch**: `feat/007-auth-users-keys-audit` | **Date**: 2026-09-01 | **Spec**: [spec.md](spec.md)

## Summary

Replace the transitional static administrator token with two independently verified identities:
Cloudflare Access JWTs for browser requests and prefix-indexed, Argon2id-verified Coire API keys for
programmatic API/MCP requests. Persist local users, entitlements, keys, UTC monthly usage windows,
and richer append-only audit records in PostgreSQL. Central authentication covers the full route
table except liveness/readiness; route dependencies then enforce roles and scopes. Rate limits use
atomic database windows, token budgets use existing gateway usage accounting, and key/user
revocation is checked during streams so it takes effect without a credential cache delay.

## Technical Context

**Language/Version**: Python 3.13; generated TypeScript strict API types  
**Primary Dependencies**: FastAPI, Pydantic v2, SQLAlchemy async, asyncpg, httpx, PyJWT with crypto,
argon2-cffi, OpenTelemetry  
**Storage**: PostgreSQL 17 via reversible Alembic migration `0009`  
**Testing**: pytest unit/contract; composed API-key/JWT integration; strict mypy/Ruff; route sweep;
OpenAPI freshness  
**Target Platform**: distroless linux/arm64 API and MCP containers on core behind Cloudflare Access  
**Project Type**: shared contracts plus control-plane API, gateway, and MCP authentication  
**Performance Goals**: cached JWT verification and API-key lookup add <=20 ms p95; revocation affects
the next request and the next bounded stream credential check  
**Constraints**: no trusted forwarding headers; no plaintext credential persistence/logging; no
credential-result cache; health/readiness are the only anonymous routes; UTC calendar-month budgets  
**Scale/Scope**: one household lab, tens of users/keys, low hundreds of requests per minute, one API
replica today without relying on replica-local correctness

## Constitution Check

- **I — Bare engines: PASS.** Authentication wraps gateway/control routes and does not change engine
  invocation or expose an engine endpoint.
- **II — Core hosts no models: PASS.** Identity, policy, and audit stay in core services; no model or
  Metal work is introduced.
- **II-a — One service/container: PASS.** Existing API and MCP images remain separate, distroless,
  non-root, read-only services; no combined identity service is added.
- **III — Contracts first: PASS.** User, key, entitlement, quota, principal, and audit wire shapes
  begin in `coire-core`; OpenAPI and generated TypeScript are updated together.
- **IV — Zero implicit trust: PASS.** Issuer, audience, signature and expiry are verified locally;
  keys are scoped, rate-limited, budgeted and revocable; all required outcomes are audited.
- **V — Models are data: PASS.** Authentication only narrows who may use existing published registry
  identifiers and does not create an acquisition path.
- **VI — Observable: PASS.** Auth outcomes, limit/quota refusals and audit failures receive bounded
  metrics, spans, structured logs, dashboard panels and alerts without secret attributes.
- **VII — Spec/test gated: PASS.** Route-table, collision, rotation, revocation, last-admin, quota,
  stream termination, redaction, contract and composed tests are explicit gates.

Post-design re-check: PASS. Database-authoritative key and usage state avoids replica-local trust;
one configured bootstrap email creates the first local admin row without accepting the legacy bearer
after migration. The design introduces two MIT dependencies, documented in `research.md`, and no
constitutional exception.

## Project Structure

```text
packages/coire-core/src/coire_core/models/auth.py
packages/coire-core/src/coire_core/models/audit.py
packages/coire-core/src/coire_core/settings.py
apps/coire-api/alembic/versions/0009_identity.py
apps/coire-api/src/coire_api/auth.py
apps/coire-api/src/coire_api/identity/
apps/coire-api/src/coire_api/routes/admin_identity.py
apps/coire-api/src/coire_api/routes/me.py
apps/coire-api/src/coire_api/gateway/{proxy.py,usage.py}
apps/coire-api/src/coire_mcp/main.py
apps/coire-api/tests/{unit,contract}/
tests/integration/test_identity_integration.py
deploy/compose/{compose.yaml,README.md,prometheus/rules/}
deploy/observability/grafana/dashboards/
docs/runbooks/identity.md
```

**Structure Decision**: extend the existing shared-contract and API persistence boundaries. The API
owns identity verification and policy; MCP reuses the same verifier through its own process; gateway
usage remains the token-accounting source. No separate auth service, Redis, or edge-only trust is
introduced.

## Complexity Tracking

No constitutional violations.
