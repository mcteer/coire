# Tasks: Auth, Users, API Keys, and Audit

**Input**: Design documents from `/specs/007-auth-users-keys-audit/`

**Tests**: Required by the specification and Constitution Principle VII. Write each listed test before
the implementation it guards and observe the intended failure.

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 Add exact MIT-licensed `PyJWT[crypto]==2.13.0` and `argon2-cffi==25.1.0` dependencies with rationale comments in `apps/coire-api/pyproject.toml` and update `uv.lock`
- [x] T002 [P] Add Cloudflare issuer/audience/JWKS TTL/leeway and bootstrap-admin-email settings in `packages/coire-core/src/coire_core/settings.py` and tests in `packages/coire-core/tests/test_settings.py`
- [x] T003 [P] Add identity/authentication metric names and bounded label policy to `specs/007-auth-users-keys-audit/research.md`

---

## Phase 2: Foundational (Blocking Prerequisites)

- [x] T004 Define strict User, Role, Entitlement, API-key, scope, quota, principal, and request models in `packages/coire-core/src/coire_core/models/auth.py`
- [x] T005 Export identity contracts from `packages/coire-core/src/coire_core/models/__init__.py` and cover validation/secret projection in `packages/coire-core/tests/test_auth_models.py`
- [x] T006 Extend append-only audit contracts with actor type, request identity, before/after, and context in `packages/coire-core/src/coire_core/models/audit.py` and `packages/coire-core/tests/test_audit_models.py`
- [x] T007 Add User, Entitlement, APIKey, RateWindow, UsageAccumulator, and compatible Audit columns in `apps/coire-api/src/coire_api/db.py`
- [x] T008 Create reversible identity schema migration with indexes, constraints, enum creation/drop, and legacy audit backfill in `apps/coire-api/alembic/versions/0009_identity.py`
- [x] T009 Add migration upgrade/downgrade assertions for every identity table/index/column in `apps/coire-api/tests/unit/test_migrations.py`
- [x] T010 Create identity package boundaries and UTC window helpers in `apps/coire-api/src/coire_api/identity/__init__.py` and `apps/coire-api/src/coire_api/identity/windows.py`
- [x] T011 Implement configured first-admin seeding and bootstrap audit in `apps/coire-api/src/coire_api/identity/bootstrap.py`
- [x] T012 Add bootstrap idempotency and configured-email tests in `apps/coire-api/tests/unit/test_identity_bootstrap.py`

**Checkpoint**: Shared contracts and schema are ready; all story work can build on them.

---

## Phase 3: User Story 1 — Every route is authenticated (Priority: P1) 🎯 MVP

**Goal**: Every non-health application route accepts only a locally verified active user/key and
unmatched, malformed, expired, wrong-issuer, and wrong-audience identities are refused.

**Independent Test**: Sweep the API/MCP route tables without credentials, then validate positive and
negative locally signed Access assertions against a test JWKS server.

- [x] T013 [P] [US1] Write JWT/JWKS cache, claim-validation, refresh, skew, and unmatched-user tests in `apps/coire-api/tests/unit/test_access_jwt.py`
- [x] T014 [P] [US1] Write full anonymous route-table sweep and health/readiness exception tests in `apps/coire-api/tests/contract/test_auth_route_sweep.py`
- [x] T015 [P] [US1] Write `/api/v1/me` and local-user admin contract tests in `apps/coire-api/tests/contract/test_admin_identity.py`
- [x] T016 [US1] Implement bounded async JWKS retrieval and strict PyJWT verification in `apps/coire-api/src/coire_api/identity/access.py`
- [x] T017 [US1] Replace transitional principal resolution with request-attached typed principals and independent failure auditing in `apps/coire-api/src/coire_api/auth.py`
- [x] T018 [US1] Install central authentication middleware, health allowlist, startup bootstrap, and RFC 9457 auth errors in `apps/coire-api/src/coire_api/app.py`
- [x] T019 [US1] Implement transactional user create/list/update/deactivate with normalized email and last-admin advisory lock in `apps/coire-api/src/coire_api/identity/users.py`
- [x] T020 [US1] Implement `/api/v1/me` and admin user routes from the contract in `apps/coire-api/src/coire_api/routes/me.py` and `apps/coire-api/src/coire_api/routes/admin_identity.py`
- [x] T021 [US1] Require authentication on MCP application traffic while retaining anonymous readiness in `apps/coire-api/src/coire_mcp/main.py`
- [x] T022 [US1] Remove static `admin_token` authentication from validation-error accounting and resolve its typed principal in `apps/coire-api/src/coire_api/app.py`

**Checkpoint**: US1 independently proves SC-001, SC-005, and SC-008.

---

## Phase 4: User Story 2 — Issue, scope, rotate, and revoke keys (Priority: P1)

**Goal**: Admins manage one-time-secret API keys; scopes are enforced and key/user revocation ends new
requests and active streams without accepting prefix collisions.

**Independent Test**: Create a chat key, prove its secret is projected once, refuse admin use, rotate
and reject the old secret, revoke and reject the new secret, and terminate a stream after revocation.

- [x] T023 [P] [US2] Write key format, Argon2 verification, collision, and one-time projection tests in `apps/coire-api/tests/unit/test_api_keys.py`
- [x] T024 [P] [US2] Write key create/list/rotate/revoke and scope contract tests in `apps/coire-api/tests/contract/test_admin_keys.py`
- [x] T025 [P] [US2] Write in-flight stream revocation and owner-deactivation gateway tests in `apps/coire-api/tests/unit/test_gateway_auth.py`
- [x] T026 [US2] Implement CSPRNG key issuance, multi-candidate prefix lookup, Argon2id verification, rotation, and revocation in `apps/coire-api/src/coire_api/identity/keys.py`
- [x] T027 [US2] Add reusable scope dependencies and stable 401/403 problem codes in `apps/coire-api/src/coire_api/auth.py`
- [x] T028 [US2] Add key list/create/rotate/revoke routes and one-time secret response handling in `apps/coire-api/src/coire_api/routes/admin_identity.py`
- [x] T029 [US2] Declare and enforce `chat`, `images`, `images:explicit`, `mcp`, and `admin` scopes across `apps/coire-api/src/coire_api/routes/v1.py`, `apps/coire-api/src/coire_api/routes/models.py`, and `apps/coire-api/src/coire_mcp/main.py`
- [x] T030 [US2] Revalidate key/user state between streamed gateway events and emit a terminal auth error on invalidation in `apps/coire-api/src/coire_api/gateway/proxy.py`
- [x] T031 [US2] Ensure all auth logs/spans retain only principal/key UUID and non-secret prefix in `apps/coire-api/src/coire_api/identity/keys.py` and `apps/coire-api/src/coire_api/auth.py`

**Checkpoint**: US2 independently proves SC-002 and SC-006.

---

## Phase 5: User Story 3 — Distinguishable rate and budget controls (Priority: P2)

**Goal**: Per-key request windows and monthly token budgets are atomic, visible, automatically roll at
UTC boundaries, and produce different actionable errors.

**Independent Test**: Exhaust a fixed-minute window and a monthly budget independently, inspect both
problem responses and key usage, advance the clock across a month, and change a budget mid-period.

- [x] T032 [P] [US3] Write UTC minute/month boundary, leap/year rollover, and budget-change tests in `apps/coire-api/tests/unit/test_identity_windows.py`
- [x] T033 [P] [US3] Write concurrent atomic rate admission and distinct rate/quota problem tests in `apps/coire-api/tests/unit/test_key_limits.py`
- [x] T034 [P] [US3] Write usage projection and automatic monthly rollover contract tests in `apps/coire-api/tests/contract/test_admin_keys.py`
- [x] T035 [US3] Implement atomic PostgreSQL rate-window admission with retry timestamps in `apps/coire-api/src/coire_api/identity/limits.py`
- [x] T036 [US3] Implement monthly accumulator lookup, quota precheck, and actual-token settlement in `apps/coire-api/src/coire_api/identity/limits.py`
- [x] T037 [US3] Enforce key rate/quota before gateway work and settle prompt/completion usage on all outcomes in `apps/coire-api/src/coire_api/gateway/usage.py` and `apps/coire-api/src/coire_api/gateway/proxy.py`
- [x] T038 [US3] Project current consumption/reset and apply budget/rate changes without resetting usage in `apps/coire-api/src/coire_api/identity/keys.py` and `apps/coire-api/src/coire_api/routes/admin_identity.py`
- [x] T039 [US3] Add RFC 9457 `rate_limit_exceeded` and `monthly_quota_exceeded` bodies plus `Retry-After` in `apps/coire-api/src/coire_api/app.py`

**Checkpoint**: US3 independently proves SC-003 and FR-011–FR-014.

---

## Phase 6: User Story 4 — Complete append-only audit trail (Priority: P2)

**Goal**: Admin/credential/entitlement mutations and authentication failures are queryable with safe
actor/context/before/after data, and no application path mutates or removes an audit row.

**Independent Test**: Perform every admin mutation and representative auth failures, map each to one
audit record, scan stored JSON/logs for presented secrets, and prove no write/delete audit route exists.

- [x] T040 [P] [US4] Write recursive key/value credential redaction and truncation tests in `apps/coire-api/tests/unit/test_audit.py`
- [x] T041 [P] [US4] Write entitlement lifecycle and audit list/filter contract tests in `apps/coire-api/tests/contract/test_admin_audit.py`
- [x] T042 [P] [US4] Write an administrative mutation-to-audit coverage sweep in `apps/coire-api/tests/contract/test_admin_audit_coverage.py`
- [x] T043 [US4] Extend audit writer projections and sanitize known Coire/JWT credential patterns in `apps/coire-api/src/coire_api/audit.py`
- [x] T044 [US4] Implement grant/revoke entitlement service with transactional audit in `apps/coire-api/src/coire_api/identity/entitlements.py`
- [x] T045 [US4] Add entitlement routes and append-only audit list/get filters in `apps/coire-api/src/coire_api/routes/admin_identity.py`
- [x] T046 [US4] Add actor-aware before/after audit writes to every identity mutation in `apps/coire-api/src/coire_api/identity/users.py`, `apps/coire-api/src/coire_api/identity/keys.py`, and `apps/coire-api/src/coire_api/identity/entitlements.py`
- [x] T047 [US4] Update pre-007 admin mutation call sites to pass typed actor/request context in `apps/coire-api/src/coire_api/routes/`

**Checkpoint**: US4 independently proves SC-004 and SC-007.

---

## Phase 7: Polish, Integration, and Operational Gates

- [x] T048 [P] Add auth/rate/quota/audit spans, bounded metrics, and structured fields in `apps/coire-api/src/coire_api/telemetry.py` and identity modules
- [x] T049 [P] Add identity dashboard panels and authentication/audit-failure alerts in `deploy/observability/grafana/dashboards/cluster.json` and `deploy/compose/prometheus/rules/coire-identity.yml`
- [x] T050 Add Keychain secret wiring and document issuer/audience/bootstrap settings in `deploy/compose/compose.yaml`, `scripts/coire-secrets-init.sh`, and `deploy/compose/README.md`
- [x] T051 [P] Write deployment, diagnosis, revocation, last-admin, bootstrap, and rollback procedures in `docs/runbooks/identity.md`
- [x] T052 Regenerate and verify `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`
- [x] T053 Add composed JWT/JWKS, key lifecycle, scope, limit/quota, stream revoke, route sweep, and secret-scan acceptance in `tests/integration/test_identity_integration.py`
- [x] T054 Run `quickstart.md`, full non-integration and composed integration suites, Ruff, mypy, web tests/lint, and OpenAPI freshness checks
- [x] T055 Build changed production images and pass image policy, CRITICAL CVE scans, and SBOM generation
- [x] T056 Complete a convergence audit against every FR/SC and append any uncovered work to `specs/007-auth-users-keys-audit/tasks.md`
- [ ] T057 Complete PR description with spec link, dependency licences/rationale, and Principles I–VII compliance

---

## Dependencies & Execution Order

- Phase 1 precedes the shared schema in Phase 2; Phase 2 blocks all user stories.
- US1 establishes real principals and users. US2 depends on US1 principals/users. US3 depends on US2
  key verification. US4 can begin after US1, then integrates US2/US3 mutation actors.
- Observability/docs can proceed alongside story work after their event names stabilize; generated
  contracts, composed acceptance, convergence, image gates, and PR handoff follow all stories.

## Parallel Opportunities

- T002–T003, T013–T015, T023–T025, T032–T034, T040–T042, and T048–T049/T051 touch independent files.
- Contract/model tests can be authored while the corresponding service implementation is pending.
- Dashboard/alert and runbook work can proceed in parallel once metric/error contracts are fixed.

## Implementation Strategy

1. Land dependency/configuration, contracts, migration, and bootstrap foundation.
2. Deliver US1 as the security MVP and validate the anonymous route sweep before key work.
3. Add complete key lifecycle/scopes, then limits/budgets, then enriched audit/entitlements.
4. Run composed acceptance and all repository/image gates; converge before review handoff.

All 57 tasks use the required checkbox, sequential ID, optional `[P]`, story label where applicable,
and an explicit repository file path.
