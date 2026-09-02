# Tasks: Agent Harness and Capability Profiles

## Phase 1: Setup

- [X] T001 Pin the MIT-licensed Pydantic AI slim OpenAI dependency in `apps/coire-agent/pyproject.toml` and `uv.lock`
- [X] T002 [P] Create harness/profile/evaluation package structure under `apps/coire-agent/src/coire_agent/`
- [X] T003 [P] Add harness settings and documented limits in `packages/coire-core/src/coire_core/settings.py` and `deploy/compose/README.md`

## Phase 2: Foundational contracts and persistence

- [X] T004 Add strict profile, strategy, run, result, context, and evaluation contracts in `packages/coire-core/src/coire_core/models/harness.py`
- [X] T005 [P] Add contract invariant and serialization tests in `packages/coire-core/tests/test_harness_models.py`
- [X] T006 Add append-only evaluation persistence and exact-variant verification fields in `apps/coire-api/src/coire_api/db.py`
- [X] T007 Add reversible harness evaluation migration in `apps/coire-api/alembic/versions/0008_harness_evaluations.py`

## Phase 3: User Story 1 — Capability-selected tools (P1)

**Independent Test**: one profile completes through native and delimited strategies with diagnosed retries.

- [X] T008 [P] [US1] Add strategy/parser/reasoning retry tests in `apps/coire-agent/tests/test_strategies.py`
- [X] T009 [P] [US1] Define coding/general/image/ops profiles and flat tools in `apps/coire-agent/src/coire_agent/profiles/`
- [X] T010 [US1] Implement normalized native, JSON, and delimited tool strategies in `apps/coire-agent/src/coire_agent/strategies.py`
- [X] T011 [US1] Implement reasoning extraction, retry ceilings, and diagnostics in `apps/coire-agent/src/coire_agent/harness.py`
- [X] T012 [US1] Implement gateway-only Pydantic AI model adapter in `apps/coire-agent/src/coire_agent/gateway_model.py`
- [X] T013 [US1] Add on-demand tool-pack loader with schema guards in `apps/coire-agent/src/coire_agent/tools.py`

## Phase 4: User Story 2 — Validated structured output (P1)

**Independent Test**: invalid output retries, repairs through admin gateway, or fails with a typed diagnosis.

- [X] T014 [P] [US2] Add structured validation/reduced-schema/repair tests in `apps/coire-agent/tests/test_outputs.py`
- [X] T015 [US2] Implement typed output validation and reduced-schema feedback in `apps/coire-agent/src/coire_agent/outputs.py`
- [X] T016 [US2] Implement fail-open admin-model repair abstraction in `apps/coire-agent/src/coire_agent/repair.py`
- [X] T017 [US2] Wire sampling, stop sequences, and thinking caps through `apps/coire-agent/src/coire_agent/harness.py`

## Phase 5: User Story 3 — Bounded long context (P2)

**Independent Test**: oversized history/tool output produces an append-only, bounded transmitted view.

- [X] T018 [P] [US3] Add context/truncation/summary fallback tests in `apps/coire-agent/tests/test_context.py`
- [X] T019 [US3] Implement append-only history and head/tail tool truncation in `apps/coire-agent/src/coire_agent/context.py`
- [X] T020 [US3] Implement pinned-admin rolling summary with unavailable fallback in `apps/coire-agent/src/coire_agent/context.py`
- [X] T021 [US3] Emit visible context budget/truncation records from `apps/coire-agent/src/coire_agent/harness.py`

## Phase 6: User Story 4 — Evaluation and write gate (P1)

**Independent Test**: scorecards append per variant; passing exact variant verifies; unverified write is refused pre-call.

- [X] T022 [P] [US4] Add evaluation service and write-gate unit tests in `apps/coire-api/tests/unit/test_harness_evaluations.py`
- [X] T023 [P] [US4] Add admin evaluation API contract tests in `apps/coire-api/tests/contract/test_admin_evaluations.py`
- [X] T024 [US4] Implement four-category deterministic evaluator in `apps/coire-agent/src/coire_agent/evals.py`
- [X] T025 [US4] Implement append-only scorecard service and exact-variant verification in `apps/coire-api/src/coire_api/evaluations.py`
- [X] T026 [US4] Implement admin start/list/get evaluation routes in `apps/coire-api/src/coire_api/routes/admin_evaluations.py`
- [X] T027 [US4] Register evaluation routes and enforce write routing gate in `apps/coire-api/src/coire_api/app.py`
- [X] T028 [US4] Add `coire eval harness` CLI in `apps/coire-api/src/coire_api/cli.py`
- [X] T029 [US4] Add composed harness/evaluation integration in `tests/integration/test_agent_harness.py`

## Phase 7: Image isolation, observability, and release

- [X] T030 Split user and ops distributions and add `apps/coire-agent/ops.Dockerfile`
- [X] T031 Add structural tests proving user image/package has no admin client in `apps/coire-agent/tests/test_image_boundary.py`
- [X] T032 Add harness spans, bounded metrics, and redacted logs in `apps/coire-agent/src/coire_agent/telemetry.py`
- [X] T033 Add harness dashboard panel and failure/regression alerts under `deploy/observability/`
- [X] T034 [P] Document observe/kill/rollback/evaluation in `docs/runbooks/agent-harness.md`
- [X] T035 Regenerate OpenAPI and TypeScript types in `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`
- [X] T036 Run Python/web/OpenAPI/compose gates and fix failures
- [X] T037 Build both harness images, enforce policy, generate SPDX SBOMs, and scan CRITICAL CVEs
- [X] T038 Run composed integration and three-strategy deterministic scorecards
- [ ] T039 Run three-model Studio scorecards and record evidence in `specs/010-agent-harness-profiles/review.md`
- [X] T040 Run convergence, finish appended tasks, commit, push, and open a draft PR

## Dependencies

Setup → foundation → US1. US2 and US3 depend on the shared harness; US4 depends on contracts and
the evaluator. Image/observability/release gates follow all stories. T039 is the only manual real-model gate.

## Phase 8: Convergence

- [X] T041 CRITICAL derive tool/output/reasoning policy and exact-variant write verification from registry-owned data, not caller assertions, per Constitution V and FR-004/FR-005/FR-007/FR-017/FR-018 (contradicts)
- [X] T042 CRITICAL make `coire eval harness <variant-id>` execute and persist the deterministic four-category suite, classifying model-load failures as infrastructure errors, per FR-015 and US4/AC1 (partial)
- [X] T043 Wire Pydantic AI profile agents to their distinct typed outputs and flat/on-demand tools per FR-003/FR-010/FR-011 (partial)
- [X] T044 Include an actual reduced schema in structured-output retry feedback and honor native/JSON/delimited output strategies per FR-005/FR-006 (partial)
- [X] T045 Reject nested-union tool schemas for prompted strategies per FR-010 and edge-case tool schema guard (partial)
- [X] T046 Emit the harness evaluation regression metric consumed by the alert per Constitution VI (partial)
- [X] T047 Align the authenticated admin start/list/get API and OpenAPI contract with evaluation execution semantics per plan: admin API (partial)
- [X] T048 Exercise the harness through the composed authenticated `/v1` gateway and scorecard persistence API per Constitution VII and T029/T038 (partial)
