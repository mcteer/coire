# Implementation Plan: MCP Server — Research, Plan, Apply

**Branch**: `feat/013-mcp-server` | **Date**: 2026-09-26 | **Spec**: [spec.md](spec.md)

## Summary

Expose exactly three authenticated coding tools at `/mcp` using the official Python MCP SDK and the existing separate `coire-mcp` container. Each call creates a Studio agent run. A node prepares a unique repository clone and typed harness request before the run; the run container can reach only its gateway relay. Research and plan have read-only tools; apply requires a harness-verified variant, creates a feature branch, commits, runs discovered tests, and returns a bounded diff plus a retrievable branch artifact. A disconnect cancels the run and revokes its token.

## Technical Context

**Language/Version**: Python 3.13, Pydantic v2; TypeScript for generated REST types
**Primary Dependencies**: Official `mcp` Python SDK, existing FastAPI, Pydantic AI, SQLAlchemy async, DBOS run workflow, httpx, git on Studio host
**Storage**: Postgres call ownership and artifact metadata; per-run Studio repository, separate writable result directory, and bounded branch artifact with expiry
**Testing**: pytest unit and contract, composed integration with a tiny model, MCP client protocol test, image policy and CVE scan
**Target Platform**: Separate hardened MCP container on core; agent containers and workspace preparation on Apple Silicon Studios
**Project Type**: Authenticated remote tool service
**Performance Goals**: Cancellation stops a run within 5 seconds; response bounded by configured run timeout
**Constraints**: Exactly three tools; no Studio engine port exposure; no runner internet access; no default-branch push; bounded clone, diff, logs, and artifacts; no extra permanent container
**Scale/Scope**: One isolated clone per call, concurrent applies to one source, one MCP process/container initially

## Constitution Check

| Principle | Design compliance |
|---|---|
| I. Bare engines | MCP uses the gateway and existing Studio node path; no engine integration or exposed engine listener. |
| II. Core hosts no user harness | Core hosts only the MCP protocol service. All coding runs execute on Studios. |
| II-a. One service, one container | Reuse the optional `coire-mcp` image/container with its healthcheck and hardening. |
| III. Contracts first | Add strict `coire-core` MCP, workspace, run request, and artifact models before service code; regenerate OpenAPI/TS for REST additions and contract-test all boundaries. |
| IV. Zero implicit trust | Authenticate every MCP request with scoped API keys, check identity and model entitlement, prepare only approved repository sources, preserve per-run token and admin kill, audit calls and artifacts. |
| V. Models are data | Resolve registry IDs through admission. Require harness verification for apply; permit eligible unverified published variants only for read-only research and plan. |
| VI. Observable | Add `coire.mcp.*` spans, `coire_` call/cancel/workspace metrics, structured fields, dashboard panel, alert, and runbook. Baseline metrics work with diagnostics off. |
| VII. Spec-driven, test-gated | This plan precedes implementation. Contract and tiny-model integration tests cover all three tools, isolation, cancellation, and verification. |

**Post-design check**: The Phase 1 contracts retain all seven principles. No exception or ADR is required. The new protocol dependency must be exactly pinned in `uv.lock` and its license stated in the PR.

## Project Structure

The feature PR also adds a root README that orients new operators and contributors to the
three-host deployment, local checks, optional profiles, and detailed runbooks.

### Documentation

```text
specs/013-mcp-server/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── contracts/mcp.md
├── quickstart.md
└── tasks.md
```

### Source Code

```text
packages/coire-core/src/coire_core/models/  # MCP and workspace wire contracts first
apps/coire-api/src/coire_mcp/            # exactly three MCP tools, auth, cancellation
apps/coire-api/src/coire_api/            # run admission, ownership, artifacts, internal calls
apps/coire-api/src/coire_scheduler/      # reuse durable run workflow and kill
apps/coire-node/src/coire_node/          # clone preparation, artifact collection, cleanup
apps/coire-agent/src/coire_agent/        # bounded coding tools and typed task results
deploy/compose/                          # existing optional MCP service and routing
tests/                                   # contract, unit, composed integration
docs/runbooks/ and deploy/observability/ # operations, dashboard, alert
```

**Structure Decision**: Extend current service boundaries. MCP handles protocol and caller identity; API/scheduler owns run state; Studio node owns workspace bytes; Studio run container owns model-driven file operations.

## Delivery Order

1. Define `coire-core` contracts and compatibility notes for read/write run class, workspace preparation, MCP inputs/results, cancellation, and branch artifact retrieval.
2. Implement node-side bounded per-run workspace preparation and cleanup, with no caller-controlled paths or clone command strings; deliver a typed request file before container creation.
3. Implement coding tools and distinct research, plan, and apply validators. Read calls mount the repository read only and write their result to a separate bounded output mount. Apply creates and commits on a feature branch, discovers allowlisted tests, and collects a bundle even if tests fail. An unavailable test runtime is reported as unsupported.
4. Extend run admission for read-only unverified variants while retaining the apply gate, then expose owner-scoped internal create/wait/cancel/result and artifact APIs through the durable workflow.
5. Mount the official SDK's Streamable HTTP app at `/mcp`, advertise only three tools, enforce API key MCP scope and origin checks on every request, and propagate disconnect cancellation to the run kill path.
6. Add telemetry, dashboard, alert, runbook, contracts and integration evidence, regenerate generated types, build/scan images, and record validation in `quickstart.md`.

## Complexity Tracking

No constitution exceptions. A downloadable branch artifact is necessary because a branch left only in a Studio clone is not usable by the caller; see [research.md](research.md).
