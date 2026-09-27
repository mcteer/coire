# Implementation Plan: Placement Scheduler and Auto-Unload

**Branch**: `feat/004-placement-scheduler` | **Date**: 2026-08-31 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/[###-feature-name]/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

Add a Postgres-authoritative per-node memory ledger, serialized admission and eviction decisions,
bounded busy-drain handling, pinning, request leases, and per-model idle TTL. DBOS owns durable load
placement and TTL workflows; the API remains the authenticated node-network boundary through a
database command handoff. The gateway updates request leases/last-used stamps around every proxy.

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: Python 3.13

**Primary Dependencies**: FastAPI, Pydantic v2, SQLAlchemy 2 async, asyncpg, DBOS 2.24, httpx, OpenTelemetry

**Storage**: PostgreSQL 17 via additive Alembic migration

**Testing**: pytest unit/contract; composed tiny-model integration; strict mypy/Ruff; OpenAPI freshness

**Target Platform**: core Linux/arm64 containers orchestrating macOS Apple Silicon node agents

**Project Type**: multi-service control plane with native node agents

**Performance Goals**: zero over-admission under concurrent load; TTL unload within one loop interval; admission decision under 250 ms excluding engine load/unload

**Constraints**: reservations, never instantaneous RSS, govern admission; 16 GiB sandbox standing reservation; pinned/in-flight occupants are never evicted; stale/unhealthy nodes block admission; core runs no model

**Scale/Scope**: two Studio nodes, tens of resident model reservations, bounded concurrent gateway requests

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I Bare engines**: PASS. Scheduler calls existing coire-node engine lifecycle only; no wrapper.
- **II Core hosts no models**: PASS. Core stores decisions and proxies commands; Metal remains on Studios.
- **II-a One service/container**: PASS. No new service or image; scheduler/API retain separate roles.
- **III Contracts first**: PASS. Ledger, placement, refusal, lease, and command shapes begin in coire-core; OpenAPI regenerated.
- **IV Zero implicit trust**: PASS. Admin mutations retain guards/audit; internal scheduler commands are not exposed publicly.
- **V Models are data**: PASS. Placement consumes registry variant IDs and verified copies only.
- **VI Observable**: PASS. Admission/eviction/TTL spans, bounded metrics, dashboard panels, and drift alert ship together.
- **VII Spec/test gated**: PASS. Contract/unit tests precede implementation and composed tiny-engine integration is required.

Post-design re-check: PASS. Per-node advisory locks, request leases, and DB command handoff preserve
the service and trust boundaries without exceptions or new dependencies.

## Project Structure

### Documentation (this feature)

```text
specs/004-placement-scheduler/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Delete unused options and expand the chosen structure with
  real paths (e.g., apps/admin, packages/something). The delivered plan must
  not include Option labels.
-->

```text
packages/coire-core/src/coire_core/models/placement.py
apps/coire-api/src/coire_api/placement/
apps/coire-api/src/coire_api/routes/admin_ledger.py
apps/coire-api/src/coire_api/gateway/proxy.py
apps/coire-api/src/coire_scheduler/placement.py
apps/coire-api/alembic/versions/0006_memory_ledger.py
apps/coire-api/tests/{unit,contract}/
tests/integration/test_placement_scheduler.py
deploy/compose/{prometheus/rules,grafana/provisioning/dashboards}/
docs/runbooks/placement.md

ios/ or android/
└── [platform-specific structure: feature modules, UI flows, platform tests]
```

**Structure Decision**: Extend coire-core contracts first, API persistence/routes and gateway
leases second, and scheduler-owned DBOS policy workflows third. The existing node engine API is
reused unchanged; no node-side scheduler authority is introduced.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
