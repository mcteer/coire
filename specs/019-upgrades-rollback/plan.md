# Implementation Plan: Upgrades and Rollback

**Branch**: `feat/019-upgrades-rollback-plan` | **Date**: 2026-10-10 | **Spec**: [spec.md](spec.md)

**Input**: `specs/019-upgrades-rollback/spec.md`, refreshed against merged 018.

## Summary

Extend immutable Studio installations and frozen control-plane releases with typed release compatibility, durable maintenance fences, independent recovery, and console/CLI operations. PostgreSQL records authority; scheduler DBOS workflows coordinate; a separate hardened Core executor and a coire-node bootstrap helper perform only accepted fixed operations. Failed activation restores exact source identities and proves recovery before reopening admission.

Planning stops before implementation. The obsolete dirty worktree `/Users/mcteer/Projects/coire-019` stays untouched. Its code may be examined for ideas, but no task completion or acceptance result transfers to this branch.

## Technical Context

**Language/Version**: Python 3.13; strict TypeScript/React for console; existing shell only for administrative bootstrap wrappers, never in production images.

**Primary Dependencies**: Existing FastAPI, Pydantic v2, SQLAlchemy async, Alembic, DBOS in scheduler only, httpx and telemetry libraries; native pinned MLX/mlx-lm/JACCL via existing installer. No new third-party dependency.

**Storage**: PostgreSQL authoritative operations/releases/fences/events; one additive reversible migration after 0033; private fsynced executor journals and immutable release manifests; existing protected secret generations and user-data stores.

**Testing**: pytest unit/contract, real disposable PostgreSQL/Compose integration, native isolated tiny-model tests, web test/lint/build, OpenAPI freshness, image build/scan/SBOM, authorized real-Studio acceptance.

**Target Platform**: Core OrbStack/Linux arm64 containers; Studios macOS 26.2+ arm64 native node runtime. No model or Metal work on Core.

**Project Type**: Existing monorepo control plane plus one dedicated deployment executor service.

**Performance Goals**: Drain ≤900 seconds before abort; readiness ≤120 seconds per target/service; recovery ≤300 seconds with available host; successful gateway outage ≤300 seconds; progress replay ≤10 seconds after API readiness. At most one active cluster operation.

**Constraints**: Exact immutable identities; no arbitrary code/path execution; no production DB downgrade; no forced termination of user work; no unknown hold release; no runtime Docker exec/pull/build/prune or new volume/network creation; API/scheduler cannot own their sole recovery mechanism.

**Scale/Scope**: Two Studios, one Core project, one active upgrade/rollback; fixed first-party application cohort. Public `/v1` shapes and exactly three MCP tools stay unchanged.

## Constitution Check

Pre-research and post-design checks both use Constitution 2.0.0. The native-daemon alternative was rejected during research; no constitutional exception is requested.

| Principle | Before research | Post-design compliance |
|---|---|---|
| I — Bare engines | Required | coire-node owns candidate smoke, PID/port/reservations and stop proof; bare engines only. |
| II — Core/Studio roles | Required | Core recovery executor is its own container; no Core weights/Metal or user harness. Studio helper is part of node lifecycle; local journal is recoverable execution evidence. |
| II-a — Bare, separate service | Required | Executor has distinct distroless image, one process, non-root, read-only rootfs, dropped capabilities, healthcheck, narrow writable coordination mounts, scan and SBOM. No shell or Docker CLI. |
| III — Contracts first | Required | Release, operation, fence, inventory, command and receipt models in coire-core precede services; OpenAPI and generated TypeScript updated together. |
| IV — Zero implicit trust | Required | Live human-admin initiation, operation-scoped execution credential, authenticated node commands, auditable mutations, current actor revalidation before activation, bounded redacted evidence. |
| V — Measured capability | Required | Smoke uses already verified registry assets; full environment identity invalidates stale qualifications; no implicit model acquisition or Apply bypass. |
| VI — Observable | Required | Durable audit/events, OTel spans/metrics/logs, panel and stalled/recovery alerts in lean and diagnostics modes. |
| VII — Spec/test gates | Required | Story-based contract, race/restart, disposable migration/Compose, tiny-model and real-cluster gates; no implementation during this planning run. |

**Repository permission gate:** Before changing deployment networking, obtain approval for the concrete executor memberships documented in `contracts/executor.md`. AGENTS.md explicitly requires asking before widening a network setting. This is an implementation authorization gate, not an unresolved architecture choice. No network configuration has been changed by planning.

## Architecture and Phases

### Phase 0 — Research completed

[research.md](research.md) records existing installer/deployer evidence, workload inventory gaps, rollback compatibility, proxy constraints and rejected native-daemon design. The user confirmed bounded gateway maintenance; spec A1 records that answer and proposed timing defaults. No unresolved technical choice remains.

### Phase 1 — Contracts and design

[data-model.md](data-model.md) defines release records, operations, fences, receipts and state transitions. [contracts/admin-api.md](contracts/admin-api.md), [contracts/node.md](contracts/node.md) and [contracts/executor.md](contracts/executor.md) define all new interfaces. [quickstart.md](quickstart.md) gives acceptance scenarios and commands. All wire models use `ConfigDict(extra="forbid")` and existing CoireError/RFC 9457 mapping.

### Phase 2 — Implementation sequence (for the next model)

1. Establish the clean merged baseline, shared contracts, persistence, immutable release validation and ownership protocol. Include the network approval gate before deployment edits.
2. Implement persistent fences and strict inventories at Core and node admission points; establish bootstrap recovery before accepting jobs.
3. Implement US1 node failure/restoration, then US2 success, manual rollback, exact qualification and paired smoke.
4. Implement US3 the separate Core executor, compatibility/backup checks, migration and sequential rollout, whole-cohort restoration and bootstrap handover.
5. Implement US4 routes/CLI/console and durable progress. Add cross-cutting telemetry, runbooks, generated contracts and acceptance evidence.

US1 is the first functional increment, not permission to omit US2–US4 or ship without acceptance. Node and Core paths share foundations; node work need not wait for Core rollout internals.

## Execution Boundaries

### Release preparation and bootstrap

Existing trusted build/installer/deployer paths prepare immutable artifacts outside request handling. A release manifest records full artifact hashes, source revision, platform, supported source release IDs, schema/journal/privacy protocol ranges, service image digests and smoke requirements. Registration validates evidence; a request cannot introduce arbitrary URLs, local paths, Compose YAML or commands. Version labels are display-only.

Install recovery support first under the ordinary administrative deployment workflow. The first managed source must already include 019 admission/protocol/bootstrap support; the initial 018-to-019 bootstrap is not retroactively a managed operation. The Studio helper's known-good interpreter and its source remain retained; launchd runs recovery before node resumption. For Core, provision the executor image, private journal volume and frozen manifest/coordination mounts. An interrupted first install remains not ready. Later executor upgrades retain a restartable incumbent supervisor through passive candidate health, fenced ownership handover and a 120-second stability check; failure restores the incumbent, including across host restart. This boundary is tested separately from application rollout.

### Maintenance and drain

Accept one cluster operation transactionally, record the actor and idempotency key, acquire ordered canonical admission locks, and persist a monotonic maintenance generation. Close affected Core admission and send authenticated local fences before collecting strict inventories. A rollout that changes the API, web gateway, scheduler or schema fences both Studios and renewal-dependent Core jobs. If only unrelated services change, derive the affected admission scope from the fixed service dependency map and drain only that scope; unrelated chat remains live and must pass the streaming test. Node rollout affects that Studio; required paired qualification also fences and drains the peer before activation, without changing its release. All owners check the fence inside their admission critical section. Background resumption follows the same rule.

Drain active requests, jobs and renewal obligations without cancellation. Idle resident engines and workers may stop through their normal owner after positive idleness. Keep standing failover reservations, distinguish them explicitly from active work, and require a valid election state. Unknown inventory, owner errors and unproven stop retain holds and block. Deadline expiry restores admission to the unchanged source only after reconciliation. Reboot does not expire a fence into permission to run work.

### Studio activation

Reserve staging disk; validate the candidate and controlled interpreter links; import/CLI smoke; fenced registry-resolved ten-token smoke with reservation and cleanup; journal intent; atomically select candidate; restart node by validated PID/creation identity; verify exact digest and fresh serving. If paired capability is declared, run real two-rank collective against the peer's exact runtime before success. Failed checks restore only the changed Studio and prove source health. A failed old-pair recheck keeps paired readiness disabled, even if ordinary single-node recovery succeeds.

Changing a runtime invalidates associated inference/Apply, image, training/evaluation and link qualification according to existing capability gates. Preserve historical records and publish no fabricated success. Ordinary serving may resume after its required smoke, while workloads needing fresh measured profiles remain unavailable with a reason. Manual rollback repeats all gates and requires a tested source compatibility record.

### Core rollout

The managed cohort is `coire-file-worker`, `coire-ops`, `coire-mcp`, `coire-scheduler`, `coire-api`, `coire-web`, omitting disabled services and unchanged image/config digests. The order is declared in the prepared plan, validated against dependencies, and fixed at acceptance. `coire-migrate` is a separate one-shot before any replacement. PostgreSQL, proxy, executor, tunnel, observability, container runtime, networks and volumes are outside this cohort. Other infrastructure upgrades use their own operational procedure.

After scope-appropriate maintenance/drain, require a validated backup receipt and compatibility of both source and candidate with the resulting schema and native formats. Revalidate the actor and issue a phase-bound grant before migration; repeat authorization before first runtime replacement. Execute migration, retain bounded output, and require terminal zero plus a typed observed-schema receipt from the migration entrypoint. Any uncertain migration state stays fenced; no application replacement or production downgrade follows.

For each changed service, verify the source container's project/service labels, image and mounts; stop it after drain, retain it, create a deterministic candidate with only approved frozen settings, and verify health/digest. Preserve DNS aliases and exact ports. On failure, remove only owned candidates and restart every stopped source in reverse order. Post-rollout functional smoke includes authenticated API/DB, scheduler reconciliation, MCP, web/proxy and a gateway tiny request on a Studio through an operation-scoped maintenance exemption. That exemption admits only named smoke work under normal accounting, never general user work. Core executor coordinates the exemption after API readiness; it never loads a model itself.

Commit a selected-release receipt in the shared coordination directory, deduplicate DB events, regain valid failover lease/hand-back, then release maintenance. Host `coire-up/down` reconcile that receipt to the host `current` symlink before later actions. Source containers may be removed without deleting volumes only after successful commit; retained manifests, images, credentials and volumes remain protected for later manual rollback. Cleanup must preserve at least current and last compatible source.

### Trust, observability and configuration

Human-admin routes reuse live key/user revalidation. Node commands use existing authenticated Core-to-node transport and a typed operation generation. The executor holds a Keychain-provisioned, operation-role-scoped API credential for claim/receipt routes; it cannot initiate admin operations. Claims are bound to project, release digests, cohort, generation, allowed phases and deadlines. Revocation blocks first activation; after activation, restoration to the recorded source remains authorized.

Local journals store intent before effect and observed identity after effect using atomic replace/fsync; bounded receipts are uploaded idempotently. API/scheduler startup honors persistent maintenance before admitting any work. Recovery always inspects reality before replay. Uncertainty retains maintenance and emits an alert.

Add settings for enablement (default off until bootstrap verification), drain/readiness/recovery deadlines, journal/manifest/coordination mounts, configured API/proxy origins and the executor credential file in shared Settings; document every environment variable in `deploy/compose/README.md`. Runtime configuration comes from environment/Compose secrets. Immutable manifests and journals are typed operation data, not alternate configuration sources.

## Project Structure

```text
specs/019-upgrades-rollback/
  spec.md, plan.md, research.md, data-model.md, quickstart.md, tasks.md, handoff.md
  checklists/requirements.md
  contracts/admin-api.md, contracts/node.md, contracts/executor.md
packages/coire-core/src/coire_core/
  models/upgrades.py                 # new wire contracts
  deploy.py, settings.py, errors.py, client.py                         # new narrowly typed upgrade client
apps/coire-api/
  alembic/versions/0034_upgrades.py   # recheck head at implementation
  src/coire_api/upgrades/            # new service, compatibility, inventory, telemetry
  src/coire_api/routes/admin_upgrades.py
  src/coire_api/routes/internal_upgrades.py
  src/coire_api/db.py, app.py, nodes_client.py                         # new narrowly typed upgrade client
  src/coire_scheduler/upgrades.py, maintenance.py
apps/coire-upgrade-executor/         # new dedicated workspace service/image
  pyproject.toml, Dockerfile
  src/coire_upgrade_executor/{main,executor,docker,journal,bootstrap,telemetry}.py
apps/coire-node/
  install.sh, install_runtime.py
  src/coire_node/{maintenance,upgrade_executor,upgrade_guard,upgrade_smoke}.py
  src/coire_node/routes/upgrades.py
  src/coire_node/agent.py, link_probes.py, training/, rendering_identity.py
apps/coire-web/src/
  api/client.ts, api/schema.d.ts
  pages/UpgradesPage.tsx, hooks/useEventStream.ts, App.tsx
scripts/coire-deploy.py
deploy/compose/{compose.yaml,coire-up,coire-down,README.md}
deploy/launchd/com.coire.node-upgrade.plist.template
deploy/observability/{alerts/upgrades.yaml,grafana/dashboards/upgrades.json,tests/upgrades.test.yaml}
docs/runbooks/upgrades.md
docs/adr/0015-upgrade-recovery-executor.md  # verify next unused ADR number
```

Existing scheduler code lives under `apps/coire-api/src/coire_scheduler`; do not create a duplicate scheduler application. New filenames above are design targets, not claims that files already exist. Tests follow existing root `tests/unit`, `tests/contract`, `tests/integration` and web conventions.

## Complexity Tracking

No constitutional violation is accepted. One new service is required because replacing API/scheduler processes otherwise removes their recovery owner. Direct, narrowly allowed Docker HTTP calls avoid a shell image and a general deployment service. The explicit shared deployment lock and bootstrap handover are necessary to preserve recovery across host restart; they are not optional polish.
