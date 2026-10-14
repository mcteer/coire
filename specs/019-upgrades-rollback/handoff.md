# 019 Implementation Handoff

Prepared 2026-10-10 for the user's requested model switch. **Implementation is now in progress.** This planning handoff is historical; use [tasks.md](tasks.md) and the [execution record](execution-record.md) for current completion and measured evidence. The feature has not met all release gates.

## Working copy and baseline

- Workspace: `/Users/mcteer/Projects/coire`.
- Branch: `feat/019-upgrades-rollback-plan`, from merged 018 at `3a872ef8f82b2a0c73969bc433b4616d079227c4`.
- Feature selector: `.specify/feature.json` points to `specs/019-upgrades-rollback` and is ignored by Git. The Spec Kit scripts may print the directory slug as BRANCH; use `git branch --show-current` for the actual branch.
- Preserve the separate dirty `/Users/mcteer/Projects/coire-019` worktree on `feat/019-upgrades-rollback`. It is based on old image-generation commit `3676e975`, contains substantial uncommitted work and cannot supply current completion marks or acceptance evidence. Do not overwrite, reset, merge or bulk-port it.

## Planning artifacts

Specify/clarify produced the revised [spec](spec.md) and [16-item quality checklist](checklists/requirements.md). Plan produced [plan](plan.md), [research](research.md), [data model](data-model.md), three [contracts](contracts/admin-api.md) and [validation guide](quickstart.md). At the planning handoff, [tasks.md](tasks.md) contained 68 unchecked implementation tasks and an explicit mapping of all 30 functional requirements and 12 success criteria. The final read-only analysis report is delivered in the completing conversation turn, rather than written by the analyze skill.

Task breakdown: setup 2, foundation 16, US1 10, US2 7, US3 17, US4 7, cross-cutting/release gates 9. Four independent test-authoring pairs are marked parallel. US1 is the first demonstrable increment; all stories and release gates remain required.

## Decisions and authorization

The user explicitly confirmed a brief, announced gateway maintenance window after active requests drain, with clear retry guidance and no forced termination of training or agent work. Default limits are 900 seconds to drain, 120 seconds for target/service readiness, 300 seconds for recovery and successful gateway outage; events resume within 10 seconds of API readiness.

A separate hardened Core executor provides recovery while API/scheduler containers are replaced. A coire-node bootstrap helper provides Studio recovery after agent or host restart. Both need independent, retained recovery ownership. The first managed source must already implement 019 bootstrap/admission/protocol support; the original 018-to-019 installation uses the existing controlled bootstrap workflow.

**Network authorization:** The user explicitly approved [executor-topology-proposal.yaml](executor-topology-proposal.yaml) on 2026-10-11: only the executor joins existing `coire-docker`, `coire-internal` and `coire-telemetry` networks, with the reviewed private mounts, secret and hardened limits. This satisfies T002/T049's authorization gate and must not be requested again. [contracts/executor.md](contracts/executor.md) retains the fixed-operation authority. No public ports, proxy flags, raw socket mount, firewall rules or capabilities are to be added. Full composed acceptance is still required.

Preserve the user's existing 018 decision: published datasets and trained adapters survive opt-out/deletion; unexported contributions remain excluded and are purged under existing policy. Automatic rollback is restricted to declared tested compatibility; it cannot silently cross pre-018 privacy or native journal boundaries.

## Implementation cautions

- Current installer and deploy tooling already provide immutable environments/releases; extend them. The node health metrics' best-effort empty inventories cannot prove idle.
- Fence every admission/resumption owner, including 016–018 training/measurement/evaluation/extraction/import and feedback/private cleanup. Drain scheduler-renewed jobs before replacing the scheduler. Never clear uncertain holds or force work to stop.
- Bind qualifications to full environment identity, not version strings; preserve historical rows and artifact hashes. Real paired collective is mandatory for declared paired capability.
- The executor uses exact allowed Docker HTTP operations and a fixed frozen-Compose subset. Source containers pin existing volume references until commit. No new volume/network creation, Docker exec, pull, build or prune through this path.
- Shared host/executor ownership and selected-release receipts must protect later coire-up/down/cleanup. Do not put a container PID into the old host PID lock.
- New migration is expected to be 0034, after 0033; recheck before implementing. New ADR is expected to be 0015. Do not reuse old-worktree numbering.
- Retain additive schema on binary rollback. Backup/validation receipt is a prerequisite, never permission to overwrite newer data. Revalidate the actor separately before migration and runtime activation.
- Bootstrap executor handover must retain a restartable incumbent supervisor until the candidate proves ownership; a short-lived host script alone cannot recover after reboot.
- Core failover election and hand-back remain authoritative. Do not suppress them for upgrades or promise zero downtime.

## Resume

Run `$speckit-implement` on this branch and process `tasks.md`. Use [quickstart.md](quickstart.md) for required checks and real-cluster acceptance. No implementation check was run or marked passing in this planning session. Planning validation covers artifact structure, requirement/task mapping and consistency only. Do not mark work complete with skipped/failing required tests, and do not deploy or merge until the implementation's full definition of done is satisfied.
