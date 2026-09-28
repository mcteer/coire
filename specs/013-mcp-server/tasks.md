# Tasks: MCP Server — Research, Plan, Apply

**Input**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/mcp.md`, `quickstart.md`

**Tests**: Required by the spec and Principle VII. Contract tests cover every new boundary; composed integration uses a tiny model.

## Phase 1: Setup

- [X] T001 Add and exactly pin the official Python MCP SDK in apps/coire-api/pyproject.toml and uv.lock; record its license and why the dependency is needed in specs/013-mcp-server/review.md
- [X] T002 Verify the existing optional MCP image, nginx route, healthcheck, and resource limits in apps/coire-api/docker/mcp.Dockerfile, apps/coire-web/nginx/nginx.conf, and deploy/compose/compose.yaml

## Phase 2: Foundational contracts and persistence

**Goal**: Typed, owner-scoped call, workspace, and run contracts before service changes.

- [X] T003 Add strict MCP input/result, citation, test-summary, call-state, and artifact models in packages/coire-core/src/coire_core/models/mcp.py
- [X] T004 Add strict workspace prepare/cleanup and artifact transfer node command models in packages/coire-core/src/coire_core/models/node.py
- [X] T005 Extend AgentRunCreate and RunContainerCreate with task class, prepared-request identity, and separate output mount identity in packages/coire-core/src/coire_core/models/runs.py, preserving old caller defaults
- [X] T006 Add one reversible Alembic migration for MCP call/result/artifact metadata and indexes in apps/coire-api/alembic/versions/0014_mcp_calls.py
- [X] T007 Add strict wire-model contract tests in apps/coire-api/tests/contract/test_mcp_contracts.py and verify current OpenAPI freshness; regenerate apps/coire-web/src/api/schema.d.ts with the REST endpoint in T017

## Phase 3: User Story 1 — Research, plan, apply loop (P1)

**Goal**: A standard MCP client completes the three-tool loop and gets a usable branch, diff, and test summary.

**Independent test**: Use a disposable sample repository and tiny model; import the returned branch artifact into a second clone and inspect the commit.

- [X] T008 [US1] Write MCP three-tool input/output contract tests in apps/coire-api/tests/contract/test_mcp_contracts.py
- [X] T009 [US1] Implement owner-checked MCP call/result/plan-ID persistence in apps/coire-api/src/coire_api/mcp_calls.py
- [X] T010 [US1] Implement bounded HTTPS source validation, immutable revision resolution, and unique per-run clone preparation on the Studio in apps/coire-node/src/coire_node/workspaces.py
- [X] T011 [US1] Add typed workspace prepare/cleanup routes with node auth and idempotent run IDs in apps/coire-node/src/coire_node/routes/workspaces.py
- [X] T012 [US1] Deliver a validated HarnessRunRequest before container create and mount a task-class-appropriate repository plus separate writable output directory in apps/coire-api/src/coire_api/run_executor.py and apps/coire-node/src/coire_node/runs.py
- [X] T013 [US1] Implement bounded read/search and read-only citation extraction tools in apps/coire-agent/src/coire_agent/tools.py and apps/coire-agent/src/coire_agent/coding.py
- [X] T014 [US1] Implement task-specific research/plan output validators and prior-result handling in apps/coire-agent/src/coire_agent/pydantic_runtime.py and apps/coire-agent/src/coire_agent/__main__.py
- [X] T015 [US1] Implement bounded patch/test/git-commit tools, generated feature branch names, structured argv test-runner allowlist, and passed/failed/not_found/unsupported reporting in apps/coire-agent/src/coire_agent/coding.py and apps/coire-agent/Dockerfile; record shipped git executable license in specs/013-mcp-server/review.md
- [X] T016 [US1] Collect a committed branch artifact capped at 64 MiB, digest, diff excerpt capped at 1 MiB, and test result even when tests fail; retain a clone on collection failure for bounded recovery in apps/coire-node/src/coire_node/workspaces.py and apps/coire-api/src/coire_api/mcp_calls.py
- [X] T017 [US1] Add owner-scoped branch artifact download and expiry cleanup routes in apps/coire-api/src/coire_api/routes/mcp_artifacts.py; regenerate apps/coire-api/openapi.json and apps/coire-web/src/api/schema.d.ts
- [X] T018 [US1] Wire research, plan, and apply to existing API/scheduler run creation, wait, and result collection in apps/coire-api/src/coire_mcp/tools.py
- [X] T019 [US1] Add composed loop with the CI tiny-model acquisition and fake engine, concurrent apply, missing-tests, failing-tests, and artifact-import tests in tests/integration/test_mcp_loop.py; the separate real tiny-model gate remains in T036

## Phase 4: User Story 2 — Narrow authenticated surface (P1)

**Goal**: The remote endpoint advertises only three tools and refuses unauthenticated or unscoped calls.

**Independent test**: Enumerate tools with an MCP client, then try missing credentials, a non-MCP key, and absent chat/admin/image names.

- [X] T020 [US2] Add MCP protocol, key-scope, identity, origin, entitlement, and method-enumeration contract tests in apps/coire-api/tests/contract/test_mcp_security.py
- [X] T021 [US2] Mount the official SDK Streamable HTTP ASGI app with correct host lifespan and exactly three tools in apps/coire-api/src/coire_mcp/main.py
- [X] T022 [US2] Enforce bearer API key with MCP scope on every MCP request and reject unapproved origins/oversized bodies in apps/coire-api/src/coire_mcp/main.py
- [X] T023 [US2] Keep nginx routing limited to the MCP endpoint and health probe in apps/coire-web/nginx/nginx.conf

## Phase 5: User Story 3 — Sandboxed and killable calls (P1)

**Goal**: Each call inherits Studio placement, run limits, a single gateway relay, timeout, kill, and attribution.

**Independent test**: Start a long apply, disconnect and separately admin-kill it; both stop within five seconds and revoke the run token.

- [X] T024 [US3] Add node workspace/mount isolation and timeout contract tests in apps/coire-node/tests/contract/test_mcp_workspace.py
- [X] T025 [US3] Harden repository/output mounts against symlink escape and unauthorized writes while retaining per-run internal networking in apps/coire-node/src/coire_node/runs.py and apps/coire-agent/src/coire_agent/__main__.py
- [X] T026 [US3] Propagate MCP HTTP disconnect to owner-scoped run kill and token revocation in apps/coire-api/src/coire_mcp/tools.py and apps/coire-api/src/coire_api/routes/runs.py
- [X] T027 [US3] Record tool, owner, duration, and outcome on AgentRun and expose them through the run listing in apps/coire-api/src/coire_api/runs.py and packages/coire-core/src/coire_core/models/runs.py
- [X] T028 [US3] Add composed disconnect, timeout, admin kill, and MCP-only restart tests in tests/integration/test_mcp_lifecycle.py

## Phase 6: User Story 4 — Verified apply, permissive reads (P2)

**Goal**: An unverified but otherwise eligible model can read; only a harness-verified variant can write.

**Independent test**: The same unverified published model succeeds for research/plan and is refused for apply before a Studio run starts.

- [X] T029 [US4] Add read/write admission and entitlement contract tests in apps/coire-api/tests/contract/test_mcp_model_admission.py
- [X] T030 [US4] Resolve eligible published variants for read-only tasks and require harness verification for apply in apps/coire-api/src/coire_api/runs.py
- [X] T031 [US4] Enforce the selected task class and verification a second time in apps/coire-agent/src/coire_agent/harness.py and apps/coire-agent/src/coire_agent/__main__.py
- [X] T032 [US4] Add tiny-model read/write verification gate integration coverage in tests/integration/test_mcp_loop.py

## Phase 7: Polish and operational proof

- [X] T033 Add MCP, workspace, artifact, and cancellation spans/metrics/structured fields in apps/coire-api/src/coire_mcp/telemetry.py and apps/coire-node/src/coire_node/workspaces.py
- [X] T034 [P] Add a baseline MCP panel and failure/cancellation alert in deploy/observability/grafana/dashboards/runs.json and deploy/observability/alerts/container-runs.yaml
- [X] T035 [P] Document startup, visibility, kill, cleanup, expiry, and rollback in docs/runbooks/mcp-server.md and config variables in deploy/compose/README.md
- [ ] T036 Run Ruff, mypy, pytest unit/contract, web type/lint/test, OpenAPI freshness, tiny-model integration, image build/policy/CVE scan; record results and license in specs/013-mcp-server/review.md and quickstart.md
- [X] T037 Write and verify the root README.md with topology, development checks, deployment profiles, repository map, and operator links; include it in the feature 013 PR

## Dependencies

```text
Setup T001–T002 → contracts T003–T007 → US1 T008–T019
                                     ↘ US2 T020–T023
                                     ↘ US3 T024–T028
                                     ↘ US4 T029–T032
All stories → operational proof T033–T036
```

US2 authentication must be complete before exposing the MCP endpoint publicly. US3 uses the run and workspace contracts from the foundation. US4 changes admission shared by US1, so its gate tests must pass before the full loop is accepted.

## Parallel examples

- After T003–T007, T008 and T020 can be authored in separate test files while T010 starts the node workspace path.
- T013 and T015 touch different coding modes but share apps/coire-agent/src/coire_agent/coding.py; implement sequentially. T034 and T035 can run in parallel after telemetry names are fixed.

## Implementation strategy

First complete the foundation and US1 on a sample repository, then prove the narrow surface, cancellation, and verification gates before making the endpoint available. Keep `coire-mcp` in its optional profile until all acceptance tests pass.
