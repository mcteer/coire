# Tasks: Gateway and OpenAI-Compatible /v1

**Input**: Design documents from `/specs/003-gateway-openai-v1/`

**Tests**: Contract, unit, and tiny-model integration tests are required by the specification and Principle VII.

## Phase 1: Setup (Shared Infrastructure)

- [X] T001 Add gateway configuration defaults and validation in `packages/coire-core/src/coire_core/settings.py`
- [X] T002 [P] Export the feature contract fixture from `specs/003-gateway-openai-v1/contracts/openapi.yaml`
- [X] T003 [P] Add gateway test factories and fake streaming-engine fixtures in `apps/coire-api/tests/conftest.py`

## Phase 2: Foundational (Blocking Prerequisites)

- [X] T004 Define strict OpenAI, Anthropic, model-list, problem, usage, and internal resolution schemas in `packages/coire-core/src/coire_core/models/gateway.py`
- [X] T005 Export gateway wire models from `packages/coire-core/src/coire_core/models/__init__.py`
- [X] T006 Add the append-only usage record ORM entity and enums in `apps/coire-api/src/coire_api/db.py`
- [X] T007 Add a reversible usage-record migration with required indexes in `apps/coire-api/alembic/versions/0003_gateway_usage.py`
- [X] T008 [P] Add migration upgrade/downgrade assertions in `apps/coire-api/tests/unit/test_migrations.py`
- [X] T009 Implement registry UUID resolution, visibility/entitlement filtering, and resolved engine targets in `apps/coire-api/src/coire_api/gateway/resolution.py`
- [X] T010 Implement cancellation-safe usage finalization in `apps/coire-api/src/coire_api/gateway/usage.py`
- [X] T011 Implement gateway traces, structured event fields, latency/in-flight/queue metrics in `apps/coire-api/src/coire_api/gateway/telemetry.py`

## Phase 3: User Story 1 - OpenAI SDK compatibility (Priority: P1) MVP

**Goal**: List visible models and serve streaming/non-streaming OpenAI Chat Completions.

**Independent Test**: An unmodified OpenAI SDK lists models and completes a streamed chat.

- [X] T012 [P] [US1] Add contract tests for `/v1/models` and OpenAI chat response/SSE shapes in `apps/coire-api/tests/contract/test_gateway_v1.py`
- [X] T013 [P] [US1] Add resolver tests proving caller model strings never become engine parameters in `apps/coire-api/tests/unit/test_gateway_resolution.py`
- [X] T014 [US1] Implement the bounded per-engine proxy and upstream OpenAI stream parser in `apps/coire-api/src/coire_api/gateway/proxy.py`
- [X] T015 [US1] Implement `/v1/models` and `/v1/chat/completions` routes with `CurrentPrincipal` in `apps/coire-api/src/coire_api/routes/v1.py`
- [X] T016 [US1] Register the `/v1` router and RFC 9457 error mapping in `apps/coire-api/src/coire_api/app.py`
- [X] T017 [US1] Add official OpenAI SDK compatibility coverage in `tests/integration/test_gateway.py`

## Phase 4: User Story 2 - Anthropic SDK compatibility (Priority: P1)

**Goal**: Adapt Anthropic Messages requests and streaming events onto the same engine backend.

**Independent Test**: An unmodified Anthropic SDK completes a streamed multi-turn exchange by changing only its base URL.

- [X] T018 [P] [US2] Add Anthropic Messages contract and event-sequence tests in `apps/coire-api/tests/contract/test_gateway_v1.py`
- [X] T019 [P] [US2] Add prompt-role/order and response translation unit tests in `apps/coire-api/tests/unit/test_gateway_anthropic.py`
- [X] T020 [US2] Implement Anthropic request, response, and SSE translation in `apps/coire-api/src/coire_api/gateway/anthropic.py`
- [X] T021 [US2] Add `/v1/messages` to the compatible router in `apps/coire-api/src/coire_api/routes/v1.py`
- [X] T022 [US2] Add official Anthropic SDK compatibility coverage in `tests/integration/test_gateway.py`

## Phase 5: User Story 3 - Cold model waiting (Priority: P1)

**Goal**: Coordinate one cold load, keep waiting streams alive, and return bounded Retry-After errors.

**Independent Test**: Concurrent cold requests trigger one load; waiting streams survive and opt-out/timeout requests receive `503` plus `Retry-After`.

- [X] T023 [P] [US3] Add load coordination, timeout, failure, and Retry-After tests in `apps/coire-api/tests/unit/test_gateway_loading.py`
- [X] T024 [US3] Implement per-model single-flight load coordination and wait ceilings in `apps/coire-api/src/coire_api/gateway/loading.py`
- [X] T025 [US3] Add stream keep-alives and wait/opt-out handling to `apps/coire-api/src/coire_api/routes/v1.py`
- [X] T026 [US3] Add cold-load integration coverage to `tests/integration/test_gateway.py`

## Phase 6: User Story 4 - Safe model refusal (Priority: P1)

**Goal**: Refuse absent, unpublished, unready, unentitled, and adapter-suffixed identifiers without engine contact or existence disclosure.

**Independent Test**: Every refused user case is `404` with zero engine requests while an admin can resolve an unpublished registry UUID.

- [X] T027 [P] [US4] Add user/admin visibility, entitlement, malformed UUID, and no-engine-contact contract tests in `apps/coire-api/tests/contract/test_gateway_v1.py`
- [X] T028 [US4] Harden resolution and route error normalization for non-disclosure in `apps/coire-api/src/coire_api/gateway/resolution.py`
- [X] T029 [US4] Add context-window estimation and explicit limit errors in `apps/coire-api/src/coire_api/gateway/context.py`
- [X] T030 [US4] Add context-limit and engine-bound resolved-path tests in `apps/coire-api/tests/unit/test_gateway_context.py`

## Phase 7: User Story 5 - Attributable usage (Priority: P2)

**Goal**: Persist terminal accounting for successes, failures, refusals, and disconnected streams.

**Independent Test**: Completed, failed, and abandoned requests each create exactly one correctly attributed usage row.

- [X] T031 [P] [US5] Add usage finalization and exactly-once tests in `apps/coire-api/tests/unit/test_gateway_usage.py`
- [X] T032 [P] [US5] Add disconnect and engine-failure streaming contract tests in `apps/coire-api/tests/contract/test_gateway_v1.py`
- [X] T033 [US5] Integrate usage finalization and prompt/completion counters through all route outcomes in `apps/coire-api/src/coire_api/routes/v1.py`
- [ ] T034 [US5] Add persisted usage assertions for normal, failed, and abandoned integration requests in `tests/integration/test_gateway.py`

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T035 [P] Add gateway dashboard panels in `deploy/compose/grafana/provisioning/dashboards/coire-gateway.json`
- [X] T036 [P] Add overload and engine-failure alert rules in `deploy/compose/prometheus/rules/coire-gateway.yml`
- [X] T037 [P] Document configuration, diagnosis, cancellation, kill, and rollback in `docs/runbooks/gateway.md` and `deploy/compose/README.md`
- [X] T038 Regenerate OpenAPI and TypeScript API types in `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`
- [X] T039 Run format, Ruff, mypy, unit/contract, OpenAPI freshness, web, and tiny-model integration gates from `specs/003-gateway-openai-v1/quickstart.md`
- [ ] T040 Measure and record p95 gateway overhead and first-token latency in `specs/003-gateway-openai-v1/quickstart.md`

## Dependencies & Execution Order

- Setup precedes the foundational schema, migration, resolver, accounting, and telemetry work.
- Foundational work blocks every user story.
- US1 establishes the shared proxy and compatible router; US2 and US3 build on that surface.
- US4 can proceed after the foundation but must be complete before final compatibility validation.
- US5 depends on the terminal paths supplied by US1–US4.
- Polish and complete validation follow all selected stories.

## Parallel Opportunities

- T002 and T003 can run together; T008 can run alongside T009–T011 after T006/T007 exist.
- Within each story, `[P]` test tasks target distinct files or independent sections and can be prepared before implementation.
- T035–T037 are independent documentation/observability files and can run together.

## Implementation Strategy

Complete setup and foundation, then deliver US1 as the SDK-compatible MVP. Add Anthropic adaptation,
cold-load behavior, refusal hardening, and usage accounting in order, keeping every completed phase
green. Finish with observability, operational documentation, generated contracts, and measured gates.
