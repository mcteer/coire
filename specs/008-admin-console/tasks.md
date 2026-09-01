# Tasks: Admin Console

**Input**: Design documents from `specs/008-admin-console/`

## Phase 1: Setup

- [X] T001 Copy the authoritative design tokens into `apps/coire-web/src/styles/tokens.css`
- [X] T002 [P] Establish admin console test fixtures in `apps/coire-web/src/test/fixtures.ts`
- [X] T003 [P] Add backend console test factories in `apps/coire-api/tests/factories.py`

## Phase 2: Foundational

- [X] T004 Add strict console, cursor-page, event, capability, activity and Ask contracts in `packages/coire-core/src/coire_core/models/console.py`
- [X] T005 [P] Test all console contract validation and secret exclusions in `packages/coire-core/tests/test_console_models.py`
- [X] T006 Implement snapshot projection, cursor encoding and freshness rules in `apps/coire-api/src/coire_api/console/service.py`
- [X] T007 Implement admin console snapshot/SSE routing and current-role enforcement in `apps/coire-api/src/coire_api/routes/admin_console.py`
- [X] T008 Register console routes and telemetry in `apps/coire-api/src/coire_api/main.py`
- [X] T009 Add contract tests for authorization, snapshot, stream and pagination in `apps/coire-api/tests/contract/test_admin_console.py`
- [X] T010 Regenerate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`
- [X] T011 Implement generated-type API client and RFC 9457 handling in `apps/coire-web/src/api/client.ts`
- [X] T012 Implement authenticated reconnect/reconcile stream hook in `apps/coire-web/src/hooks/useEventStream.ts`
- [X] T013 [P] Test API client and stream disconnect recovery in `apps/coire-web/src/hooks/useEventStream.test.tsx`
- [X] T014 Build accessible shared Glass shell, history routing, tabs, dock and capability gating in `apps/coire-web/src/components/Shell.tsx`, `apps/coire-web/src/styles/app.css`, and `apps/coire-web/src/App.tsx`
- [X] T015 Test non-admin hiding/refusal and shared navigation in `apps/coire-web/src/App.test.tsx`

## Phase 3: User Story 1 — Cluster at a glance (P1)

**Independent Test**: Seed healthy, degraded, unreachable and stale nodes plus changing instances; the overview distinguishes all states and updates within two seconds without polling.

- [X] T016 [P] [US1] Add snapshot projection tests for capacity, disk, drift, freshness and unreachable instances in `apps/coire-api/tests/unit/test_console_service.py`
- [X] T017 [US1] Complete node/ledger/link/instance aggregation in `apps/coire-api/src/coire_api/console/service.py`
- [X] T018 [P] [US1] Build node, ledger, alert and activity tiles in `apps/coire-web/src/pages/admin/OverviewPage.tsx`
- [X] T019 [P] [US1] Build semantic status, capacity and freshness components in `apps/coire-web/src/components/Status.tsx`
- [X] T020 [US1] Test live overview rendering and state distinctions in `apps/coire-web/src/pages/admin/OverviewPage.test.tsx`

## Phase 4: User Story 2 — Model roster (P1)

**Independent Test**: Add a tiny repo/precision, observe every acquisition/copy stage, curate/publish/default it, load/unload it, and retry a seeded failure entirely through the console.

- [X] T021 [P] [US2] Add paginated/versioned model and mutation contract tests in `apps/coire-api/tests/contract/test_admin_console_models.py`
- [X] T022 [US2] Add stable model pagination and optimistic concurrency to `apps/coire-api/src/coire_api/routes/admin_models.py` and `apps/coire-api/src/coire_api/routes/admin_variants.py`
- [X] T023 [US2] Ensure acquisition progress exposes per-node bytes and resume failure detail in `packages/coire-core/src/coire_core/models/acquisition.py` and `apps/coire-api/src/coire_api/routes/admin_acquisitions.py`
- [X] T024 [P] [US2] Build roster/acquisition/variant tables and add/curate forms in `apps/coire-web/src/pages/admin/ModelsPage.tsx`
- [X] T025 [US2] Wire acquire, publish, retire, pin, load, unload, convert, default and retry actions in `apps/coire-web/src/pages/admin/ModelsPage.tsx`
- [X] T026 [US2] Test the complete model lifecycle and named confirmations in `apps/coire-web/src/pages/admin/ModelsPage.test.tsx`

## Phase 5: User Story 3 — Stop running work (P1)

**Independent Test**: Seed a running acquisition and instance; stop each by named confirmation, observe terminal state within five seconds, and find actor/target audit rows. Agent-run UI remains absent.

- [X] T027 [P] [US3] Add activity union/stop authorization and audit contract tests in `apps/coire-api/tests/contract/test_admin_activity.py`
- [X] T028 [US3] Implement paginated activity projection and allowlisted audited stop dispatch in `apps/coire-api/src/coire_api/routes/admin_console.py`
- [X] T029 [P] [US3] Build runs/jobs/instances activity table in `apps/coire-web/src/pages/admin/ActivityPage.tsx`
- [X] T030 [US3] Implement reusable four-second exact-target confirmation in `apps/coire-web/src/components/ConfirmAction.tsx`
- [X] T031 [US3] Test stop success/failure/state reconciliation and absent run controls in `apps/coire-web/src/pages/admin/ActivityPage.test.tsx`

## Phase 6: User Story 4 — Users, keys and entitlements (P2)

**Independent Test**: Create/update a user, issue a scoped limited key shown once, grant/revoke entitlement, revoke key, and verify immediate refusal plus audit entries.

- [X] T032 [P] [US4] Add pagination/version conflict coverage to `apps/coire-api/tests/contract/test_admin_identity_console.py`
- [X] T033 [US4] Add user pagination and optimistic versions to `apps/coire-api/src/coire_api/routes/admin_identity.py`
- [X] T034 [P] [US4] Build users, roles, keys, usage and entitlements interface in `apps/coire-web/src/pages/admin/IdentityPage.tsx`
- [X] T035 [US4] Implement one-time key-secret presentation and clearing behavior in `apps/coire-web/src/pages/admin/IdentityPage.tsx`
- [X] T036 [US4] Test user/key/entitlement workflows and secret one-time display in `apps/coire-web/src/pages/admin/IdentityPage.test.tsx`

## Phase 7: User Story 5 — Ask Coire (P3)

**Independent Test**: Ask a state question and receive grounded sources without any mutation; make the pinned model unavailable and receive a clear unavailable response.

- [X] T037 [P] [US5] Add read-only schema, authorization and unavailable-path contract tests in `apps/coire-api/tests/contract/test_admin_ops.py`
- [X] T038 [US5] Implement snapshot-grounded read-only Ask service in `apps/coire-api/src/coire_api/console/ops.py`
- [X] T039 [US5] Expose the no-tools Ask route in `apps/coire-api/src/coire_api/routes/admin_console.py`
- [X] T040 [P] [US5] Build Ask Coire panel and unavailable state in `apps/coire-web/src/pages/admin/AskCoire.tsx`
- [X] T041 [US5] Test grounded responses, accessibility and absence of mutation controls in `apps/coire-web/src/pages/admin/AskCoire.test.tsx`

## Phase 8: Polish and cross-cutting gates

- [X] T042 [P] Build paginated filterable audit viewer in `apps/coire-web/src/pages/admin/AuditPage.tsx`
- [X] T043 [P] Add console metrics/dashboard/alert in `deploy/observability/prometheus/rules/coire-api.yml` and `deploy/observability/grafana/dashboards/coire.json`
- [X] T044 [P] Document observe/kill/rollback procedures in `docs/runbooks/admin-console.md`
- [X] T045 Add composed end-to-end scenarios in `tests/integration/test_admin_console.py`
- [X] T046 Run and fix Python unit/contract/integration, Ruff, strict mypy and OpenAPI freshness gates
- [X] T047 Run and fix web tests, lint, typecheck, production build and Playwright WebKit validation
- [X] T048 Build production images, enforce image policy, generate SPDX SBOMs and pass critical CVE scans
- [X] T049 Run Spec Kit analysis/convergence and append/complete any discovered work in `specs/008-admin-console/tasks.md`
- [X] T050 Complete review handoff, constitution checklist and PR template in `specs/008-admin-console/review.md`

## Dependencies and execution order

Setup → foundation → US1. US2 and US3 depend on the stream/client foundation but are independently testable. US4 depends only on the shared client/shell. US5 depends on snapshot projection. Polish follows all stories. Within each story, contracts/tests precede implementation and generated types precede UI consumption.

Parallel opportunities are marked `[P]`; they touch separate test/UI/observability files. Suggested MVP is Setup + Foundation + US1, but this feature is complete only after all five stories and Phase 8 pass.

## Phase 9: Convergence

- [ ] T051 Add a truthful core-host health/capacity projection and tile per FR-002 and US1/AC1 (missing)
- [X] T052 Implement stable pagination for model and user collections and bounded incremental rendering per FR-021 (partial)
- [X] T053 Enforce and test optimistic concurrency on model and user edits per FR-019 (missing)
- [ ] T054 Complete roster curation, entitlement, validation and default-variant controls per FR-006 and FR-008 (partial)
- [ ] T055 Connect Ask Coire to an actual read-only ops harness/admin-model path with explicit unavailable degradation per FR-015 and FR-016 (partial)
- [X] T056 Add fetch-SSE disconnect, Last-Event-ID reconnect and reconciliation tests per FR-017 and SC-007 (missing)
- [X] T057 Add the console Grafana panel and execute Playwright WebKit visual/accessibility validation per Constitution VI and plan testing decision (partial)
