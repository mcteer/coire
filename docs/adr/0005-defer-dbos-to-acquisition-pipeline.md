# ADR-0005: Defer DBOS to the acquisition pipeline; drive feature 001's download job with a stage-cursor reconciler

- **Status**: Accepted
- **Date**: 2026-08-30
- **Deciders**: Dan McTeer
- **Constitution**: exception to the **Technology Constraints** line "DBOS for durable workflows"
- **Time-box**: closes with feature 002 (acquisition pipeline), which is specified as a DBOS workflow

## Context

The constitution names DBOS as the durable-workflow runtime, and feature 002 is specified as
one: inspect, pull, convert, validate, replicate, each a resumable step with real branching
(raw versus MLX-format, keep-raw, variants) and compensation (delete raw weights, roll back a
failed variant). Feature 001 needs only one linear job — pull, verify, export, import, verify —
whose long-running steps all execute on the Studios, which persist their own progress under
`/opt/coire/state` and resume it after a reboot. The control plane's share of the work is to
know which stage each job is in and to issue the node verb for that stage.

## Decision

1. Feature 001 keeps one `download_jobs` row per acquisition whose `stage` is a cursor over a
   fixed sequence. A reconciler task inside `coire-api` advances each unfinished job by issuing
   the node verb for its current stage and reading the node job's status.
2. **Every node verb is idempotent on the control plane's job id** (`POST /node/jobs/pull`,
   `/import`, `/verify`, `/node/engines`, `/node/models/{slug}/export`). A restarted control
   plane re-issues the current stage and re-attaches; a restarted node agent resumes its own
   worker from its state file. Neither restart loses or duplicates work.
3. Model state is **recomputed** from copy verification on every reconciler pass
   (`ready ⇔ two verified copies`), never set once and trusted.
4. Feature 002 introduces DBOS and wraps **these same node verbs** as workflow steps. The node
   contract (`specs/001-model-registry-node-agent/contracts/node-api.yaml`) does not change;
   the reconciler's download-job driver is replaced by the workflow, not extended. The engine
   sync and reconcile passes remain until feature 005's instance workflow absorbs them.

## Consequences

- Until 002, resumption guarantees rest on node-side persistence plus idempotent re-issue,
  which the integration suite exercises (agent restart mid-job, control-plane restart mid-job).
  There is no workflow history table; the audit log and `model_state_transitions` are the
  record.
- 002's Constitution Check must record this ADR as **closed** and must show the download job
  running under DBOS with the node verbs unchanged.
- `coire-api` gains no new runtime or schema for this; the DBOS schema arrives once, in 002.

## Alternatives rejected

- DBOS in 001 — adds its runtime and Postgres schema to `coire-api` for a workflow with no
  branching and no compensation, and fixes 002's design before 002 is planned.
- Celery/RQ/arq — a broker or queue on core for one job type; a new service for no gain.
- A hand-rolled generic workflow engine — worse than DBOS at DBOS's job; rejected on
  Principle VII ("defer generality until a second concrete use appears" — 002 is that use).
