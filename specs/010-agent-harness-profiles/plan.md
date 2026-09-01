# Implementation Plan: Agent Harness and Capability Profiles

**Branch**: `feat/010-agent-harness-profiles` | **Date**: 2026-09-01 | **Spec**: `specs/010-agent-harness-profiles/spec.md`

## Summary

Build strict shared harness/evaluation contracts, a user-facing Pydantic AI harness with coding,
general, and image profiles, and a separately built ops harness with the admin client. Select tool
and output strategies entirely from registry capability data, enforce append-only context budgeting
and verification gates, persist per-variant scorecards, and expose an admin evaluation API/CLI.

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: Pydantic AI slim OpenAI provider (MIT), existing httpx/Pydantic/SQLAlchemy/OTel
**Storage**: PostgreSQL append-only evaluation rows and variant verification projection
**Testing**: pytest unit/contract/integration with deterministic model doubles; tiny-model Studio manual scorecards
**Target Platform**: distroless arm64 agent containers on Studios; ops container on core calling the gateway
**Project Type**: shared contracts, CLI, two isolated harness images, admin API
**Performance Goals**: bounded retries; context preparation under 100 ms excluding summary calls
**Constraints**: gateway-only model access; no admin client in user image; ≤10 flat tools/profile; no content telemetry
**Scale/Scope**: four profiles, three tool/output strategies, four evaluation categories

## Constitution Check

- **I**: harness calls registry models only through `/v1`; it never controls an engine.
- **II**: user harness image runs only on Studios; the separate ops image is the sole core harness.
- **II-a**: distinct distroless images, one process, non-root/read-only/capability-dropped at runtime.
- **III**: run, profile, result, scorecard, and evaluation payloads begin in `coire-core`.
- **IV**: short-lived run credentials are inputs; user image has no admin client; telemetry excludes content.
- **V**: capability and verification are variant data; write tasks fail closed when unverified.
- **VI**: spans/metrics/logs, dashboard panel, and alert ship with the paths.
- **VII**: deterministic contracts/unit/integration plus tiny-model/manual multi-model evaluation gates.

No exception is required.

## Project Structure

```text
packages/coire-core/src/coire_core/models/harness.py
apps/coire-agent/src/coire_agent/{harness,context,strategies,profiles,evals}.py
apps/coire-agent/{Dockerfile,ops.Dockerfile}
apps/coire-api/src/coire_api/routes/admin_evaluations.py
apps/coire-api/src/coire_api/evaluations.py
apps/coire-api/alembic/versions/*_harness_evaluations.py
tests/integration/test_agent_harness.py
docs/runbooks/agent-harness.md
```

## Design Phases

1. Define strict contracts, persistence, and strategy invariants.
2. Implement context budgeting, tool/output adapters, profiles, retry/repair, and gateway model adapter.
3. Add evaluation runner, append-only scorecards, verification projection, API, and CLI.
4. Split user/ops images structurally and enforce image-content/network boundaries.
5. Add telemetry, operations docs, integration/evaluation evidence, image and convergence gates.

## Post-Design Constitution Re-check

Passed. The design adds no engine path, keeps admin code out of the user artifact, treats model
capability as persisted data, and makes verification an append-only measured result.
