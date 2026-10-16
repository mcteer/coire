# Tasks: Local browser sign-in

## Setup
- [x] T001 Specify and clarify local identity amendment in spec.md; complete plan, research, data model and contracts.
## US1 — Local sign-in
- [x] T002 Add shared local_auth contracts, settings, ORM rows and reversible 0035 migration.
- [x] T003 Add failing database-backed login/session/origin/throttle contracts in tests/contract/test_local_auth.py.
- [x] T004 Implement audited provisioning, bounded password verification, durable throttle and cookie authentication in identity/local.py and routes/local_auth.py.
- [x] T005 Add browser SignIn component, initial authentication routing and generated client types with tests.
## US2 — End access
- [x] T006 Implement logout/reset/session invalidation and regression tests including current role/entitlements/deactivation.
## Finish
- [x] T007 Add local TLS deployment support, provisioning command, runbook, dashboard panel and alert.
- [x] T008 Run security contracts, regression, lint/type/OpenAPI/web checks and build/scan changed images.
- [x] T009 Deploy through documented local workflow, complete real browser sign-in/reload/logout acceptance and remove all owned test resources; record evidence.

Dependencies: T001 → T002 → T003 → T004 → T005/T006 → T007 → T008 → T009. Independent API and browser checks may run concurrently; no agent delegation required. Deliver a complete usable login before treating this feature as done.
