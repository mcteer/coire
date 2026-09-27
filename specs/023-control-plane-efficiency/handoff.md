# Resume handoff — 2026-09-26

## User direction

Fix all worthwhile findings of the architecture/resource review across the stack, as one PR. Latest instruction: **pause after plan/tasks/analyze so the user can switch models**. Do not start implementation or live remediation until the user resumes.

## Repository and delivery

- Branch: `feat/023-control-plane-efficiency`, created from `origin/feat/012-coire-ops-confirmed-mutations` at `eb3a362` (merged feature 020, PR #23).
- Issue: https://github.com/mcteer/coire/issues/24.
- Final PR targets that integration branch. User's one-PR direction overrides contribution size/split guidance. Do not merge without new authorization for this PR.
- `.specify/feature.json` points to this directory. Only planning artifacts have changed so far; no code/services/settings/credentials changed.
- Specification, plan, research, data model, contracts, quickstart and 39-task list are present. T001 is documentation setup only; T002 onward remain unimplemented.
- Optional diagnostics conflicts with current constitution VI/technology constraints; T002 explicitly amends governance/ADR/dependent specs before implementation. Do not claim that gate already passed.

## Findings to preserve

- User's symptom is macOS desktop/window movement, not specifically the Coire page. No causal proof: zero swap, 93–95% idle follow-up CPU, ~453 MiB container totals, ~2.6 GiB helper RSS (different memory accounting).
- Live API repeatedly fails database auth (~18 failures/~1,900 log lines/minute); collector retries missing backends. Four historical backends failed bind mounts rooted in the mutable checkout. Current source and running September-1 images differ.
- Read-only credential checks: API/scheduler/Postgres mounted password bytes match; fresh API settings/URL preserve them; application-network asyncpg authentication still fails. Local psql uses trust, so local success does not validate SCRAM. Do not reset data or use wholesale secret regeneration.
- coire-up overwrites live secrets before validating all inputs; coire-down cleans only three credential files. Add protected complete generations, cross-container preflight, explicit safe recovery, release snapshots and project locks outside checkout.
- `/ready` is intentionally liveness-only. Preserve it; use sanitized `/health` and startup auth preflight. Capacity contract already has a runtime-source field.
- Run WAIT blocks the current serial executor's KILL. Kill workflow's finally block reports killed despite failure. Fix independent priority execution plus confirmed/transactionally guarded completion, including CREATE/token and normal-completion races.
- Move five executors and run/shard reconcilers to scheduler; retain API registry/node/link status caches and failover. Scheduler needs RUN_AGENT_IMAGE/RUN_RELAY_IMAGE. DBOS 2.24.0 workflows use another event loop; enforce session/event ownership.
- Source console rebuilds half-second snapshots per viewer with time-only changes and duplicate ledger projection; hidden pages keep streaming.
- Feature-009 branch has useful full monitoring configs/images/tests; selectively port, never overwrite newer Compose/auth/fabric/failover changes wholesale.

## Resume sequence

Read this directory and applicable AGENTS/constitution, inspect git status, and preserve the user pause until resumed. Then close T002 explicitly, add failing tests before code, and execute tasks with evidence. No model inference or remote Studio Docker operation is needed. Global OrbStack caps and a stop-for-comparison are documented trials, not automatic implementation actions.
