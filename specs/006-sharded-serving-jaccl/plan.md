# Implementation Plan: Sharded Serving over JACCL

**Branch**: `feat/006-sharded-serving-jaccl` | **Date**: 2026-09-01 | **Spec**: `spec.md`

## Summary

Extend feature 005 instances so `sharded:tp` and `sharded:pp` atomically reserve both declared
Studios and launch one two-rank group through coire-node using explicit `mlx.launch` argv and a
generated, inventory-derived JACCL hostfile. Persist damped link probes and append-only benchmark
results. A rank failure fails and drains the group, degrades the failed node, and makes one bounded
attempt to place a verified smaller variant on the survivor. Link latency is measured and alerted,
but per the operator decision it is not a placement threshold.

## Technical Context

**Language/Version**: Python 3.13, TypeScript strict  
**Primary Dependencies**: FastAPI, Pydantic v2, SQLAlchemy async, DBOS, httpx, MLX/JACCL  
**Storage**: PostgreSQL/Alembic; node state beneath `/opt/coire/state`  
**Testing**: pytest unit/contract/composed fake-rank tests; manual two-Mac JACCL gate  
**Target Platform**: core Linux containers plus macOS 26.2+ Apple Silicon node agents  
**Project Type**: distributed control plane and native node service  
**Performance Goals**: prompt stream must fail promptly on rank loss; benchmark records tokens/s  
**Constraints**: core is never a rank; only the direct Studio Thunderbolt fabric carries JACCL;
all internet egress remains Wi-Fi; no latency admission threshold; no shell command construction  
**Scale/Scope**: exactly two ranks today, with contracts that do not assume core participation

## Constitution Check

- **I — Bare engines: PASS.** coire-node invokes `mlx.launch` and `mlx_lm.server` directly with
  explicit argv; rank endpoints remain behind node and gateway authentication.
- **II — Core hosts no models: PASS.** core coordinates durable state only and never joins the
  hostfile or imports MLX.
- **II-a — One service, one container: PASS.** no wrapper service or combined container is added.
- **III — Contracts first: PASS.** link probes, group/rank commands, benchmark rows and API
  responses begin as strict coire-core models and generated OpenAPI.
- **IV — Zero implicit trust: PASS.** admin mutations are scoped/audited; hostfile membership is
  derived from declared nodes; callers cannot provide hosts, paths, commands or rank addresses.
- **V — Models are data: PASS.** only verified registry variants and copies become launch argv.
- **VI — Observable: PASS.** link/group/rank/benchmark spans, metrics, logs, panels and alerts ship.
- **VII — Spec-driven, test-gated: PASS WITH LIVE GATE.** Linux CI uses a fake two-rank group; real
  JACCL all-reduce, rank failure and a <=1 GB model must pass on both Studios before merge.

Post-design re-check: PASS. The design introduces no wrapper, core rank, implicit node discovery,
caller-controlled engine value, or widened network path.

## Project Structure

```text
packages/coire-core/src/coire_core/models/sharding.py
apps/coire-api/alembic/versions/0008_sharded_serving.py
apps/coire-api/src/coire_api/routes/admin_sharding.py
apps/coire-api/src/coire_scheduler/sharding.py
apps/coire-node/src/coire_node/sharding.py
apps/coire-node/src/coire_node/routes/sharding.py
deploy/cluster/
deploy/compose/prometheus/rules/
docs/runbooks/sharded-serving.md
tests/integration/test_sharding_integration.py
```

**Structure Decision**: extend the existing contract, scheduler, node-agent, API, deployment and
integration-test boundaries; do not create a sharding wrapper service.
