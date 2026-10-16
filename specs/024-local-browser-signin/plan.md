# Implementation Plan: Local browser sign-in

**Branch**: `feat/024-local-browser-signin` | **Date**: 2026-10-15 | **Spec**: [spec.md](spec.md)

## Summary
Add opt-in local email/password authentication to the existing API and browser. Reuse installed Argon2id and existing users/audit. Persist hashed sessions and login throttles in Postgres. Keep Access and API-key flows intact. LAN requires HTTPS; isolated loopback development can use HTTP.

## Technical Context
Python 3.13, FastAPI, Pydantic v2, async SQLAlchemy/Postgres17, existing argon2-cffi, React/TypeScript/nginx. No new dependencies. Eight-hour maximum sessions, one password verifier concurrently, five attempts per account per minute. One reversible migration after 0034. Tests: API contracts and real disposable Postgres, React tests and real browser acceptance. No inference behavior changes or engine tests required for this authentication feature.

## Constitution Check
I: no engine changes. II: authentication remains on Core. II-a: existing hardened API/web images only. III: shared Pydantic contracts first, regenerate OpenAPI/TS. IV: independently verified credentials, origin/host checks on all cookie mutations, random hashed sessions, audited provisioning and login/logout, no implicit roles. V: unchanged registry and entitlements. VI: spans, bounded metrics, dashboard and alert. VII: spec/plan/tasks precede code; contract, database and browser tests required. Post-design: all pass; no exception requested.

## Project Structure
Shared `coire_core/models/local_auth.py`; API `identity/local.py`, `routes/local_auth.py`, auth middleware/settings/database; `0035_local_browser_auth` migration; browser `pages/SignIn.tsx` and App integration; deployment local TLS overlay/provisioning and runbook. All additions scoped to this feature.

## Phases
1. Contracts, credential/session/throttle rows and migration.
2. Audited operator password provisioning, login/logout, cookie verifier, middleware origin protection.
3. Accessible browser form and logout; preserve provider-only behavior.
4. TLS deployment support, documentation and observability.
5. Focused security regression, type/lint/schema checks, image build/scan and real browser acceptance. Retire all owned fixtures.
