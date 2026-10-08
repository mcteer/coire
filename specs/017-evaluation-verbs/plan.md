# Implementation Plan: Evaluation Verbs

**Branch**: `feat/017-evaluation-verbs` | **Date**: 2026-10-07 | **Spec**: [spec.md](spec.md)
**Input**: `specs/017-evaluation-verbs/spec.md` | **Baseline**: merged 016, `ec8679c1a3ede4c7b4d897ab4c27b0833a517ecc`
**Status**: Implementation and 70/70 task acceptance complete under the user’s `$speckit-implement` instruction; admissions disabled, review prepared. See execution record for the remaining node collection-budget operational limitation.

## Summary

Make evaluation a durable admin operation using existing Studio sandboxes, exact registry targets and ledger admission. Reuse the harness gate, add bounded authored task and platform-judge suites, and schedule declared checkpoint/final base-versus-adapter comparisons. Preserve 016 recipes and hashes with a separate v2 training document. Checkpoint evaluation pauses trainers at a full committed checkpoint and resumes only after cleanup and current authorization/admission. Evaluation outcomes remain separate from training outcomes and never auto-publish adapters.

## Technical Context

**Language/Version**: Python 3.13; strict TypeScript/React 18 SPA.
**Primary Dependencies**: Existing FastAPI/Pydantic v2, SQLAlchemy 2/asyncpg, Alembic, DBOS 2.24.0 (scheduler only), httpx, Pydantic AI slim 2.37.0, OTel, React/Vite. Exact versions remain in `uv.lock` and the web package lockfiles (`package-lock.json` in CI, `pnpm-lock.yaml` locally); no dependency additions are planned.
**Storage**: Postgres 17 for durable intent, results and audit; bounded evidence namespace on existing private API/scheduler `coire-training-data` volume; temporary Studio input/output mounts. One reversible Alembic migration after 0031.
**Testing**: pytest unit/contract/Postgres recovery, isolated Mac tiny-model integration (≤1 GB), Vitest/testing-library, Ruff/mypy/TypeScript/ESLint/OpenAPI freshness, image policy/scans/SBOM, Prometheus rule tests, real Studio acceptance before implementation PR merge.
**Target Platform**: Existing core compose services plus macOS Studio node agents and ephemeral hardened agent containers; bare MLX serving remains node-owned.
**Project Type**: Existing monorepo control plane/CLI/web, no new service.
**Performance Goals**: Acceptance suites ≤16 cases and ≤900 s each; loaded single-node chat ≤1.5 s p95 TTFT, gateway ≤20 ms p95 excluding inference; zero swap growth/chat failures in qualifying samples; reachable cancellation ≤5 s.
**Constraints**: Exact identity grants; no core harness/tokenizer/model execution; no generated code execution; fixed scorer allowlist; existing serving protections and training admission fences; immutable v1 identity; diagnostics optional.
**Scale/Scope**: Two Studios. Default one active group cluster-wide, ≤32 cases/suite, ≤4 suites/training recipe, ≤32 distinct pre-final checkpoint updates, one active boundary/job, bounded evidence and deadlines. Settings below are ceilings, not promises that every model completes at those bounds.

## Constitution Check

Both gates are design checks, not a claim of passing implementation tests.

| Principle | Before research | After design | Evidence / implementation gate |
|---|---|---|---|
| I — Bare engines | PASS | PASS | Existing gateway/node bare model lifecycle only; no engine wrapper, direct port or auto-download. Ledger-owned load/unload and kill tests. |
| II — Core/Studio roles | PASS with existing CLI gap scoped for correction | PASS | All harness/task/judge execution moves to typed Studio workloads; core only coordinates/persists. Isolation test proves no core evaluator. |
| II-a — Container hardening | PASS | PASS | Existing agent image/entrypoint, fixed fixtures, read-only input and separate bounded output; no shell or additional service. CI policy/scan/SBOM remain gates. |
| III — Contracts first | PASS | PASS | New wire types in coire-core, separate training v2 types, versioned node envelopes, generated OpenAPI/TS and compatibility tests. Public `/v1` shape unchanged. |
| IV — Trust | PASS | PASS | Active admin owner, exact expiring READ grants, no judge tools, audited mutations/refusals, result binding, cancellation/revocation tests. |
| V — Measured capabilities | PASS | PASS | Only complete harness evidence changes exact verification; task/judge informational, no acquisition/publishing side effects. |
| VI — Observability | PASS | PASS | Durable history, low-cardinality metrics, OTel, structured content-free logs, jobs panels and baseline alerts independent of historical diagnostics. |
| VII — Spec/test gates | PASS | PASS | This artifact set precedes implementation; per-boundary contracts, tiny-model suite tests and real training/checkpoint/coexistence acceptance required before release. |

No exceptions or constitution amendment. No architecture deviation requiring an ADR. The CLI compatibility change is explicit in contracts/runbook tasks, not an exception to Principle II.

## Project Structure

```text
specs/017-evaluation-verbs/
  spec.md, plan.md, research.md, data-model.md, quickstart.md, tasks.md
  contracts/evaluation-api.md
  contracts/training-and-workers.md
  contracts/cli-and-console.md
  checklists/requirements.md
  handoff.md
packages/coire-core/src/coire_core/
  models/evaluation.py                 # new admin/result/node/workload contracts
  models/training_evaluation.py        # schedule/value types without training imports
  models/training.py, training_node.py # v1/v2 documents and embedding boundaries
  models/runs.py, node.py              # internal evaluation purpose/preparation
  evaluation_suites/                  # shared immutable authored data/manifests only
  settings.py, errors.py
apps/coire-api/src/coire_api/
  evaluation/                         # new service, catalog, evidence, comparison,
                                      # authorization, contamination, events, telemetry
  evaluations.py                      # existing exact harness verification writer
  routes/admin_evaluation_runs.py      # new suite/run/group/measurement routes
  routes/admin_evaluations.py          # legacy read compatibility / reject loose scores
  runs.py, run_executor.py, run_tokens.py, nodes_client.py
  training/{specs,checkpoints,adapters,service,retention,events}.py
  db.py, app.py, cli.py
apps/coire-api/src/coire_scheduler/
  evaluations.py, evaluation_guard.py, evaluation_measurements.py
  training_controller.py, training_admission.py, main.py, workers.py
apps/coire-api/alembic/versions/0032_evaluation_verbs.py
apps/coire-agent/src/coire_agent/
  __main__.py, evals.py
  evaluation.py, evaluation_tasks.py, evaluation_judge.py
apps/coire-node/src/coire_node/
  routes/evaluations.py, evaluations.py # bounded workspace preparation/collection
  runs.py, workspaces.py, agent.py
  training/{worker,checkpoints,retention}.py
apps/coire-web/src/
  api/evaluations.ts, api/schema.d.ts
  components/evaluations/              # submit/history/detail/comparison
  components/training/                 # form, adapters, run, checkpoint score series
  hooks/useEventStream.ts               # reuse
recipes/training/sft-evaluated.yaml
apps/*/tests/, packages/coire-core/tests/, tests/integration/
deploy/observability/{alerts,tests,grafana/dashboards}/
docs/runbooks/evaluations.md, docs/runbooks/sft-training.md
```

New paths are proposals for implementation; existing modules remain where shipped. The actual scheduler package lives in `apps/coire-api/src/coire_scheduler`, not a separate `apps/coire-scheduler` source tree. Shared suite fixtures are data in coire-core; only the Studio agent executes them.

## Phase 0 — Research conclusions

[research.md](research.md) resolves execution placement, existing verification writer, v1 hash compatibility, checkpoint ownership/fencing, target authorization, suite content/identity, judging, contamination, resource admission, evidence retention and rollout. One user clarification confirmed opt-in automatic suites. Other bounded design choices are explicitly recorded. No unresolved research or external dependency selection remains.

## Phase 1 — Design

### Contracts and persistence first

Use [data-model.md](data-model.md) and [contracts](contracts/). New wire types forbid extra fields and non-finite scores. API mutations use idempotency and expected-version controls; trigger/result uniqueness is database-enforced. Keep original training JSON, old adapters' harness `evaluation_id`, and historical harness scorecards untouched. Evaluation records snapshot target metadata so retirement cannot erase history. Add immutable suite templates/versions through the supported catalog; clients cannot submit arbitrary scorer code.

### Orchestration and phase ownership

A comparison group contains runs for declared suites. Each task/rubric run can evaluate one or two exact subjects; a pairwise run requires two; harness runs each evaluate one exact subject. Automatic groups submit base and candidate together (two linked harness runs if harness is declared). For each suite, generate base output, collect and release only phase-owned model/sandbox resources, generate candidate output, then score/judge. Each phase has a durable child run/attempt ID and scoped single-target token. The scheduler stages previous outputs as bounded data for the judge. No phase needs simultaneous subject/judge residency.

The scheduler observes/collects an existing container after restart before creating anything new. Terminal commits validate complete case IDs, requested target manifests, suite/scorer identity, bounds and phase/attempt fencing. Repeated collection is idempotent; stale/foreign evidence fails. No automatic inference retry after an ambiguous completed phase; recover its evidence or terminate with infrastructure failure. An explicit user rerun creates a fresh linked run.

Finalization writes the evaluation trigger transactionally with the ready final adapter. Full evaluation queue/evidence quota or disabled admission never rolls back successful training: the durable obligation waits without reserving a run slot, then materializes its runs when capacity permits or records explicit per-suite capacity/admission failure at its fixed deadline. Checkpoint commit creates the trigger/pin and returns an evaluation-pause decision before the trainer leaves the committed boundary. Both ranks receive that decision for distributed training. An internal checkpoint adapter is private/unverified, hidden from ordinary adapter pickers and publishing; it is retired after terminal cleanup. Its immutable metadata survives. Newer admin/protective pause or cancel always wins over automatic resume.

### Capacity, serving priority and measurements

Default one active group and one model phase cluster-wide; independent submissions queue. Reserve both engine placement and sandbox slice through current locks/ledger. A trainer and evaluation never overlap in 017. Preserve pinned and leased serving targets and existing image/exclusion policy; reusing the node run relay must not add a network or egress route. Prefer an idle node; otherwise queue unless a current evaluation-specific coexistence profile covers the exact suite workload, target/runtime/hardware and resident set.

Add a bounded admin measurement operation using the same phase executor plus existing gateway completion/latency and node telemetry primitives. Controlled qualification is explicitly marked measurement work; it requires normal memory admission, live guard data and an admin owner, but may collect its first profile without a prior profile. At least 100 baseline and 100 mixed requests per loaded resident with ≤4k input tokens, zero failures/swap, TTFT ≤1.5 s and gateway overhead ≤20 ms p95 qualify a profile. Expire after 24 hours and invalidate on target/runtime/hardware/resident/workload changes. No automatic production qualification or borrowing a training profile. Stale telemetry or breached limits stops new requests, revokes/kills the current evaluation sandbox and cleans owned instances; holds persist until stop proof. Preserve an explicit failure instead of fabricating a low score.

### Versioning and rollout

V1 training documents retain exact serialization and hashes. Define v2 documents alongside v1 in `models/training.py`; the companion `models/training_evaluation.py` contains only schedule/value types and imports no training document, avoiding a circular import at embedding boundaries. Common field helpers may be extracted internally while preserving public v1 class names and serialized shapes. V2 adds `eval.suites` and explicit pre-final `checkpoint_updates`; declared suites always run at final success independently of loss controls. Update every embedding boundary with discriminated document parsing, not coercion. Resolved v2 freezes suite content, judge targets and workload settings; v1 resolved shape stays unchanged. Historical v1 resumes on mixed releases; new v2 requires advertised compatible node/agent capabilities before admission. Disable new submissions first during rollback; keep sweeps and cancellation active. Drain or convert remaining evaluation-owned pauses into explicit admin pause before old binaries resume training.

### Evidence, configuration and observability

Proposed `COIRE_EVALUATIONS_ENABLED=false`, `COIRE_EVALUATION_MAX_ACTIVE_GROUPS=1` (initial ceiling 1), `COIRE_EVALUATION_QUEUE_TIMEOUT_SECONDS=3600`, `COIRE_EVALUATION_TIMEOUT_SECONDS=900` (suite range 60–1800), `COIRE_EVALUATION_EVIDENCE_RETENTION_DAYS=7`, `COIRE_EVALUATION_EVIDENCE_QUOTA_BYTES=1073741824`. Existing private volume root plus fixed `evaluations/` namespace; no arbitrary path in requests. Per-run cap 8 MiB, max 100 pending runs globally, case input ≤16 KiB, output ≤16 KiB, ≤4096 generated tokens/case. All settings live in `coire_core/settings.py` and `deploy/compose/README.md`; production deployment follows existing config sourcing.

Spans: `coire.api.evaluation.submit`, `coire.scheduler.evaluation.phase`, `coire.node.evaluation.prepare`, `coire.agent.evaluation.case`, `coire.scheduler.evaluation.cleanup`. Metrics: `coire_evaluation_runs_total{suite_type,outcome}`, `coire_evaluation_active{phase}`, `coire_evaluation_oldest_unresolved_seconds`, `coire_evaluation_queue_age_seconds`, `coire_evaluation_evidence_bytes`, guard failures by bounded reason. Structured logs include evaluation/run/job/instance/model/user IDs when applicable but no content. Jobs dashboard links to authenticated history. Baseline alerts cover stuck cleanup/oldest unresolved and repeated infrastructure failures; rule tests exercise diagnostics-disabled operation.

### Test and acceptance strategy

Meaningful regression gates include frozen v1 digest/checkpoint resume; migration up/down without touching training JSON; private/unverified READ evaluation without WRITE access; complete evidence binding and legacy loose-score rejection; no task/judge gate mutations; self-judge aliases and malformed-output exhaustion; comparability mismatch matrix and input-overlap checks; idempotency/recovery; cancel/deadline/owner revocation; phase-owned cleanup; checkpoint pin quota and pause/cancel races; two-rank committed-boundary pause; SSE replay/reset; operator rollback with diagnostics off.

Tiny-model CI exercises harness/task/judge execution in isolated Studio-style containers; judge contract failures can be an expected asserted outcome where a tiny model cannot produce valid scoring. A separate deterministic judge stub validates aggregation/retries, and real Studio acceptance must produce actual valid task/rubric scores. CI does not call real Studios. Real acceptance uses existing acquired/validated models plus a distinct suitable judge and records private proof references in the feature execution record. Full checks and release evidence remain implementation work described in [quickstart.md](quickstart.md).

## Phase 2 — Task generation

[tasks.md](tasks.md) orders shared contract/persistence/executor foundation before the four user-story increments. US1 delivers the roadmap's automatic final comparison; US2 exposes on-demand commands/history and the legacy harness transition; US3 adds committed-checkpoint ownership; US4 completes judge-safety presentation and adversarial acceptance. Self-judge rejection and no-tools constraints are foundational prerequisites, not delayed until US4. Cross-cutting release tasks cover schemas, docs, metrics/alerts, tiny-model and real Studio gates.

## Complexity Tracking

No constitutional violations. Reuse one agent runtime, current node/ledger/gateway control paths and existing private storage. Additional durable rows and versioned training types are necessary to preserve restart safety and existing immutable hashes. No general benchmark/plugin framework, new container service or cross-job cache is included.
