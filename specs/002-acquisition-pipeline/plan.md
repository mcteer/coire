# Implementation Plan: Model Acquisition Pipeline

**Branch**: `feat/002-acquisition-pipeline` | **Date**: 2026-08-31 | **Spec**: `specs/002-acquisition-pipeline/spec.md`

## Summary

Generalise feature 001's MLX-only acquisition cursor into a DBOS workflow owned by
`coire-scheduler`. Inspection remains metadata-only; node work is issued through authenticated,
idempotent node contracts. Raw safetensors are pulled once, converted by the bare
`mlx_lm.convert` entry point with an explicit argument vector, validated on the Studio, replicated
over the data fabric, and represented as independently publishable model variants. Existing ready
models are migrated to a default variant without moving their files.

## Technical Context

**Language/Version**: Python 3.13; React/TypeScript strict for the generated client only

**Primary Dependencies**: FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, DBOS 2.24.0,
httpx, huggingface-hub 1.29.0, mlx-lm 0.31.3

**Storage**: Postgres 17 for workflow/variant/stage/validation state; `/opt/coire/models` and
`/opt/coire/state/jobs` on each Studio for raw, partial, converted, and checkpointed node work

**Testing**: pytest unit and contract tests, composed integration with fake Hub/converter/engine,
macOS engine tests with a ≤1 GB model, real-cluster raw conversion before merge

**Target Platform**: OrbStack containers on arm64 core; native launchd agents and Metal workers on
macOS 26.2+ Studios

**Project Type**: Distributed control-plane web service plus native worker agent

**Performance Goals**: Metadata rejection before weight transfer; completed stages never rerun;
one WAN pull per revision; node collection overhead remains within the existing 2% CPU/150 MiB RSS
budget outside conversion workers

**Constraints**: No caller-supplied path reaches an engine or converter; no Hub upload path;
conversion never exceeds the node ledger or enters swap; raw data removed only after validated,
verified replicas exist; data transfer never falls back to control Wi-Fi

**Scale/Scope**: Two Studios, tens of base models, several variants per model, one active conversion
per node initially, multi-hour resumable workflows

## Constitution Check

### Pre-design gate

- **I — PASS:** only the bare `mlx_lm.convert` and `mlx_lm.server` entry points are used. The node
  owns every subprocess and records its PID/reservation before work begins.
- **II — PASS:** core stores metadata and drives DBOS; all weights and Metal work remain on Studios.
- **II-a — PASS:** no new service is introduced. DBOS runs in `coire-scheduler`; conversion runs as
  a supervised native child of coire-node. Existing images retain hardening policy.
- **III — PASS:** variant, inspection, validation, workflow, reservation, and node-job shapes begin
  in `coire-core`; OpenAPI and TypeScript are regenerated in the same change.
- **IV — PASS:** all admin routes retain the admin guard and audit trail; node routes use scoped
  node credentials. Raw paths and Hub credentials never cross the public boundary.
- **V — PASS:** only admins initiate acquisition; immutable Hub revisions and registry-generated
  variant identifiers select local paths. Validation gates publication.
- **VI — PASS:** each workflow stage emits spans, metrics, structured identifiers, a dashboard
  panel, and alerts for stuck/failed conversion and estimate drift.
- **VII — PASS:** this plan follows a clarified spec and includes contract, integration, engine,
  interruption, and real-cluster conversion gates.

No constitutional exception is required.

## Project Structure

### Documentation (this feature)

```text
specs/002-acquisition-pipeline/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── acquisition-api.yaml
│   └── node-acquisition-api.yaml
└── tasks.md
```

### Source Code (repository root)

```text
packages/coire-core/src/coire_core/models/
├── acquisition.py
├── jobs.py
└── registry.py

apps/coire-api/
├── alembic/versions/0005_acquisition_variants.py
├── src/coire_api/routes/admin_models.py
├── src/coire_api/registry/{inspection,service}.py
└── tests/{contract,unit}/

apps/coire-api/src/coire_scheduler/
├── acquisition.py
└── dbos_runtime.py

apps/coire-node/src/coire_node/
├── conversion.py
├── validation.py
├── jobs.py
├── worker.py
└── routes/jobs.py

deploy/compose/
├── compose.yaml
├── grafana/provisioning/dashboards/coire-jobs.json
└── prometheus/rules/coire-acquisition.yml

tests/integration/test_acquisition_pipeline.py
docs/runbooks/acquisition.md
```

**Structure Decision**: Extend the existing shared-contract, API, scheduler, and native-node
boundaries. Durable orchestration belongs only to `coire-scheduler`; the API submits and reads
workflow state, and the node performs idempotent physical stages.

## Migration and compatibility

Alembic creates variant, validation, workflow-stage, and reservation tables and seeds one default
variant for every existing model. Existing model UUIDs continue to mean “the default published
variant,” preserving `/v1` and CLI compatibility. Existing feature-001 download jobs remain readable
and resumable; new or retried acquisitions are adopted by DBOS using their existing job UUID as the
workflow identity. The migration is additive and reversible only while no multi-variant rows exist.

## Post-design Constitution Check

The contracts keep all paths and Hub identifiers on admin/node surfaces, define no upload verb, and
make reservation and validation state explicit. The data model preserves core as orchestration-only,
the quickstart includes failure recovery and observability evidence, and the implementation adds no
service or trust boundary. All seven gates remain PASS.
