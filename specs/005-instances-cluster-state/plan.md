# Implementation Plan: Model Instances and Cluster State

**Branch**: `feat/005-instances-cluster-state` | **Date**: 2026-08-31 | **Spec**: [spec.md](spec.md)

## Summary

Generalise feature 004's placement decisions and engine rows into durable model instances. A DBOS
workflow owns the instance state machine, node commands remain behind the authenticated API boundary,
Postgres persists ordered transitions for SSE replay, and the gateway leases/ranks ready instances
rather than models. Add typed cluster state and hashed, single-use per-node registration tokens.

## Technical Context

**Language/Version**: Python 3.13; TypeScript strict for generated client types  
**Primary Dependencies**: FastAPI, Pydantic v2, SQLAlchemy async, asyncpg, DBOS, httpx, OTel  
**Storage**: PostgreSQL 17; reversible Alembic migration `0007`  
**Testing**: pytest unit/contract; composed tiny-engine integration; mypy/ruff/OpenAPI freshness  
**Target Platform**: Distroless arm64 Linux services on core; authenticated macOS node agents  
**Project Type**: control-plane API, scheduler workflow, gateway routing, shared contracts  
**Performance Goals**: immediate 202 create; ordered SSE replay; bounded drain  
**Constraints**: no model execution on core; no discovery; one cold-load instance per concurrent key  
**Scale/Scope**: three declared hosts, two workers, several simultaneous instances per model

## Constitution Check

- **I — Bare engines**: PASS. Only coire-node starts/stops `mlx_lm.server`.
- **II — Core hosts no models**: PASS. Core persists and orchestrates only.
- **II-a — One service/container**: PASS. Existing API/scheduler roles remain separate.
- **III — Contracts first**: PASS. All instance, event, token, and cluster shapes start in coire-core.
- **IV — Zero implicit trust**: PASS. Tokens are random, hashed, node-scoped, single-use/revocable;
  every registration attempt is audited.
- **V — Models are data**: PASS. Only registry and validated variant UUIDs reach lifecycle code.
- **VI — Observable**: PASS. State spans/metrics/logs, dashboard panels, and alerts ship together.
- **VII — Spec/test gated**: PASS. Contract, state-machine, restart, drain and tiny-model tests apply.

Post-design re-check: PASS; no exceptions or ADR deviations.

## Project Structure

```text
packages/coire-core/src/coire_core/models/instance.py
apps/coire-api/alembic/versions/0007_model_instances.py
apps/coire-api/src/coire_api/{db.py,instance/service.py,routes/instances.py,gateway/}
apps/coire-api/src/coire_scheduler/instances.py
apps/coire-api/tests/{unit,contract}/
tests/integration/test_instances.py
deploy/compose/{grafana,prometheus}/
docs/runbooks/instances.md
```

**Structure Decision**: Evolve the current shared-contract/API/scheduler split. The API owns node
credentials and SSE; DBOS owns durable transitions; coire-node remains the sole engine owner.

## Complexity Tracking

No constitutional violations.
