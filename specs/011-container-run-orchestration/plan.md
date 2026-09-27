# Implementation Plan: Container Run Orchestration on the Studios

**Branch**: `feat/011-container-run-orchestration` | **Date**: 2026-09-01 | **Spec**: `specs/011-container-run-orchestration/spec.md`

## Summary

Add strict run, token, limit, lifecycle, and node-command contracts; persist durable run and
token state; place runs only on registered Studios; and have `coire-scheduler` drive a DBOS
workflow through a Docker Engine API broker owned by `coire-node`. Each ephemeral agent container
uses an isolated internal bridge shared only with a hardened gateway relay, receives a server-side
revocable credential, and is always collected and removed. Reconciliation resumes known runs and
reaps labeled orphans without granting core access to a Studio runtime.

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: existing FastAPI, Pydantic v2, SQLAlchemy async, httpx UDS transport, DBOS 2.24.0, OTel
**Storage**: PostgreSQL run/token/command state; Studio workspace/result directories and local Docker state
**Testing**: pytest unit and contract tests with a fake Engine API; Docker integration tests; manual OrbStack verification on both Studios
**Target Platform**: `coire-scheduler` on Linux/arm64 core containers; `coire-node` and OrbStack Docker 29.4.0 on macOS arm64 Studios
**Project Type**: shared contracts, control-plane API/workflow, Studio node broker, ephemeral hardened container
**Performance Goals**: responsive-node kill acknowledgement within 5 seconds; bounded log ingestion; no duplicate container across scheduler recovery
**Constraints**: no user harness/core fallback; no remote Docker socket or shell; one gateway route; default three slots and 16 GiB standing slice per Studio
**Scale/Scope**: two Studios, six concurrent runs by default, one durable workflow and one container per run

## Constitution Check

- **I**: run containers call inference only through the authenticated gateway; they neither expose nor control engines.
- **II**: placement candidates are registered Studio nodes only. Core has no agent image requirement and no run-container execution path.
- **II-a**: the existing agent image is non-root/distroless; runtime creation enforces read-only rootfs, dropped capabilities, no-new-privileges, limits, health-neutral ephemeral lifecycle, and an isolated per-run network.
- **III**: every public, scheduler, and node wire shape begins as a strict Pydantic model in `coire-core`; OpenAPI and TypeScript are regenerated.
- **IV**: run tokens are random, hashed at rest, server-side scoped/revocable/expiring; create, kill, and terminal outcomes are audited.
- **V**: permitted model IDs resolve from registry records and token scopes; no run payload can name an engine path or trigger acquisition.
- **VI**: lifecycle operations emit spans, structured run fields, bounded log metrics, a dashboard panel, and alerts.
- **VII**: contract, unit, Docker integration, recovery, confinement, and real-Studio verification gates ship with the feature.

No constitutional exception is required. The gateway relay is a narrow network enforcement
component, not an inference engine or harness: it forwards only authenticated `/v1` traffic to
`coire-api`, has no model lifecycle behavior, and is owned as part of the node run broker.

## Project Structure

```text
packages/coire-core/src/coire_core/models/runs.py
apps/coire-api/src/coire_api/{runs,run_tokens}.py
apps/coire-api/src/coire_api/routes/{runs,admin_runs}.py
apps/coire-api/src/coire_scheduler/runs.py
apps/coire-api/alembic/versions/*_container_runs.py
apps/coire-node/src/coire_node/{docker_api,runs,run_reconciler,run_relay}.py
apps/coire-node/src/coire_node/routes/runs.py
apps/coire-node/tests/{unit,contract}/
tests/integration/test_container_runs.py
deploy/observability/{grafana/provisioning/dashboards/coire-runs.json,prometheus/rules/coire-runs.yml}
docs/runbooks/container-runs.md
```

**Structure Decision**: extend the existing shared-contract, API/scheduler, and node-agent
packages. No core-side runtime client or new user-facing harness image is introduced.

## Design Phases

1. Define strict run/token/node command contracts and append-only persisted lifecycle state.
2. Implement server-side token minting, validation, revocation, scoped gateway authorization,
   authenticated run/admin routes, and audit.
3. Implement the node-local Docker API broker, hardened create configuration, bounded log/result
   handling, per-run isolated network plus gateway relay, and idempotent cleanup/reconciliation.
4. Implement deterministic Studio placement, slot queueing, DBOS lifecycle/recovery, kill-first
   token revocation, and ledger integration.
5. Add telemetry, dashboards, alerts, operational docs, Docker integration tests, image gates, and
   manual two-Studio evidence.

## Post-Design Constitution Re-check

Passed. Contracts remain authoritative, core never receives Studio runtime access, the only
container egress is an allowlisted gateway relay, credentials fail closed from database state, and
the design retains the existing bare-engine boundary.
