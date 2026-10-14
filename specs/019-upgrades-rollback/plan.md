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


Implementation safety refinement: pending training captures at most 15 seconds
of drain at acceptance, under the shared Core admission lock. Native checkpoint
commit already has a 60-second completion budget; the longer default upgrade
drain would block its replication and fail existing work. The shorter original
budget aborts unchanged before that timeout. Idle acceptance retains the
configured budget; operation replay never renews or changes the captured budget.

Acceptance-discovered lifecycle refinement (T072): a timed-out JACCL launcher
can orphan its SSH transport, leaving the peer worker exiting and unreadable.
Stop only a provenance-verified owned transport (PID birth, exact argv, UID and
socket endpoints), never an unidentified process by name. Physical absence does
not resolve a durable collective dispatch lacking its result. Add a typed,
scoped cleanup/reconciliation path that preserves the original job, generation,
command and deadline, records interrupted/failed work without inventing a
measurement, and cannot launch another collective or grant qualification. Keep
maintenance closed until actual Source serving and all required cleanup have
been proved. Test timeout, lost-response and restart boundaries, stage new
immutable artifacts rather than patching installed Sources, and document the
operator path. Constitution I/III/IV/VI/VII remain binding; no new dependency or
permission widening is justified by this refinement.

### Approved extension: distinct expired-operation recovery authority

Owner approval on 2026-10-12 permits a new administrative recovery intent, not
renewal of an accepted upgrade's deadline. Implement shared strict contracts first:
original job/generation/plan/scope references, exact Source/peer, verified immutable
support release, idempotency identity, separately captured bounded timing, durable
ownership and resolution receipts. The old operation's missed budget and failure
history remain explicit. Terminal resolution is an additive audited link; it must
not relabel the original upgrade as successful or copy qualification.

Design and test the exclusive transition from the retained recovery owner before
performing any live handover. An active legacy Source cannot use idle refresh;
its released-owner/terminal-proof prerequisites remain unchanged. New support
must run through a separately scoped immutable recovery installation/handover
producer, with actual ownership/census and retained intent before effects. Keep
known-good Source/helper artifacts and journals for rollback. No direct journal
editing, installed Source patching, implicit trust or unfenced maintenance gap.

API acceptance must authenticate an administrator, audit acceptance and effect
outcomes, and reject stale authority or conflicting reuse. New budgets are captured
once and cannot be renewed by retry. Crashes/lost responses replay the same intent;
expiry/cancellation retain unresolved obligations. Verify real Source serving,
complete physical cleanup, required exact-pair qualification and data preservation
under the new intent before the supported resolution/maintenance-release producer
may act. Add contract/unit/crash/integration tests, OTel/metrics/dashboard/alerts,
OpenAPI/web types and operational recovery documentation. No native deployment
before immutable support/handover and resolution tests pass.

Constitution Check: I/II preserve bare node-owned engines and model-free Core;
II-a retains hardened per-service images if deployment adds a service; III requires
shared contracts; IV requires admin audit, scoped authority and fail-closed transfer;
V retains verified registry assets; VI requires observability; VII requires this
approved spec/plan extension and all test/release gates. No new dependency or
permission/network exception is approved. T073 precedes T072 live reconciliation
and the affected T064–T068 release gates.

### Cold retained-owner recovery refinement (2026-10-13)

Read-only authenticated Core inspection confirms retained operation
`01M4RCBR8BT3FTK36FBWKVC1TR`, generation 57, remains recovery-required without a
finish time. Read-only Studio A inspection positively finds the recorded private
fixture supervisor, guard and Source node identities absent. Live-preservation
transfer prerequisites therefore do not hold; do not invent fresh legacy status,
use initial transfer as a fallback, restart the old supervisor, edit journals or
renew original budgets.

Implement cold recovery explicitly within T073's already required host/owner
restart coverage. A new audited intent must bind the original plan/scopes,
sealed exact Source and immutable support. Trusted producers must verify actual
retirement of all retained identities, actual exclusive ownership, unchanged
selected Source bytes and complete physical inventories before any fixed Source
restart. Journal the new effect before spawning only the verified Source node
module, record its new PID/birth under the separate intent and preserve the old
process preimages. Unknown work/processes refuse. New effects consume only the
separate once-captured budget; no old deadline, receipt or qualification changes.
Fresh serving, physical cleanup, exact-pair and data-preservation proofs plus
fenced Source-helper handback remain prerequisites to linked resolution.
Existing live-preservation transfer continues to refuse absent owners; no implicit
cold fallback is permitted. Constitution I/III/IV/VI/VII still apply, with no new
dependency, privilege or network expansion.

The recorded Source node is absent and the selected pointer still names the
candidate; the sealed Source startup gate correctly refuses the original native
`recovery_required` selection. The cold producer therefore needs a typed,
write-ahead Source-selection projection under the new audited intent: retain the
entire original native intent and bootstrap/pointer preimages immutably, verify
exclusive ownership and all known runtime bytes, then reconcile the mutable
native selection view to Source without changing any original deadline, grant,
receipt or Core failure outcome. This is a producer-owned recovery effect, not
manual journal editing or a sealed Source patch. The separate intent stays linked
to that exact history. Recovery-owner readiness behind the acknowledged local
fence is distinct from Source-serving/pair/data qualification; no ordinary work
may cross that fence. The old operation remains failed/recovery-required until
the explicit after-deadline resolution producer proves all required obligations.

Fresh smoke uses the original registry/model/environment bindings but a distinct
private recovery ownership namespace and the new captured deadline. Do not
overwrite original smoke evidence or replay interrupted collective dispatches.
Required new exact-pair qualification gets a distinct recovery command identity,
while old interrupted scopes can only be reconciled by cleanup-only authority.

Read-only inspection confirms that the retained sealed Source's inventory code
does not understand `cleanup.json`. After trusted fresh canonical local/peer
inventories prove all physical owners idle, immutable support may project that
legacy inventory namespace through a typed write-ahead archive transition. Keep
the original dispatch directory's scope/result/lock bytes unchanged under the
separate recovery's private history namespace, bind its full content digest and
each captured scope, and atomically rename only that exact original job directory.
Retain the first intent, pre-effect observations and completion receipt; require
live actor/physical ownership and original fences at each boundary. A lost rename
response reconciles the two exact before/after locations without a second probe,
invented old result, deadline change or maintenance release. Unconsumed inventory
observations may refresh within the unchanged captured root budget; historical
observations alone never authorize the namespace effect. New pair dispatches use
their own recovery scope, outside this archived legacy namespace.

Fixture Source handback retains the original supervisor descriptor in the cold
lifetime and passes that exact already-held descriptor to a fixed module in the
immutable support runtime. The child adopts the exact sealed Source node/guard
identities; parent close does not unlock the inherited open description. A typed
write-ahead adoption intent and positively verified child acknowledgement precede
local maintenance release. A portable process/receipt DTO or an independently
opened descriptor cannot replace that kernel ownership. Source readiness
publication during smoke/pair checks stops and joins before handback preparation.

Local closure retains an immutable effect receipt. Final resolution uses a
separate fresh physical observation under the unchanged captured root, avoiding
replacement of that receipt on a lost response. Fixed authenticated Core metadata
state explicitly distinguishes pending authority from an already committed linked
terminal record. Terminal inspection is read-only even after expiry/initiator
revocation; pending state grants no effects and independently requires live exact
handback. Core verifies actual Source/peer readiness outside DB transactions,
then atomically appends the late failed-upgrade event, closes slots/fences and
writes its audit. Managed service registration, succession and ordinary supervisor
restart behavior still require completion and acceptance before shipping.

Read-only inspection of the retained frozen Core cohort found revision
`0034_upgrades` with all three recovery authority tables absent. Prepare only
that positively known legacy shape through an authenticated, audited admin API:
pin the complete structural digest, original Studio failure/plan/Source and all
acknowledged fences; create the three current tables and shared immutable-history
triggers, while positively verifying the two known legacy phase checks. Keep
Alembic history, existing rows, original deadlines/receipts/slots/fences unchanged.
Unknown partial protocols or constraint preimages refuse. PostgreSQL DDL and the
audit commit atomically; a lost response returns the captured preparation receipt.

A fixed one-shot utility in the ordinary migration/API image can invoke this
same admin API in process, using the actual middleware, API-key authentication,
live role/scope checks and transaction/audit path. It opens no listener and needs
no network or privilege expansion. Its strict stdin command supplies no SQL,
paths, credential values or recovery budget; the admin key comes from an existing
Keychain-sourced mounted secret. Normal fresh installations continue to use the
single ordinary 0034 migration. This preparation is not a recovery intent.

Read-only execution of the retained immutable Core reader proved it cannot parse
a new original-operation terminal enum. Preserve its existing `rolled_back` wire
shape, reserve `expired-recovery-<id>` event identities for the linked producer,
and record `outcome="recovered_after_deadline"` and
`original_upgrade_succeeded=false` in the distinct strict resolution. Retain the
complete pre-resolution operation payload, including its old failure reason, in
that resolution. The original operation and linked event carry the explicit
`expired_operation_recovered` reason. No old receipt/deadline is changed; no
ordinary receipt can mint the reserved event. The tested old operation/event
readers accept these payloads. Existing phase constraints need no replacement.

An immutable compatibility API instance can use the ordinary hardened API image
and existing DB/telemetry/node concerns while the frozen incumbent stays intact.
Its configured original job/generation/Source tuple is non-wire request scope,
not execution authority. Expose only readiness, authenticated recovery/schema
admin routes and fixed node recovery/peer observations. Suppress ordinary API
lifespan workers in this distinct recovery concern, keep standard authentication
and live Core gates, and refuse other original failures. For isolated acceptance,
publish only a loopback fixture endpoint and use the existing authenticated SSH
transport; do not widen production CORS, networks, capabilities or firewalls.

### Expired unstarted attempt closure

An expired cold intent whose Core ledger issued no transfer, consumption, owner,
smoke, verification or handback authority may close only its separate attempt.
Use a fixed installed, authenticated node metadata observer with actual recorded
startup retirement, held read-only exclusions, stable original preimages and
unchanged native/Core data fingerprints. The observer's fixed I/O bound is not a
recovery execution budget. An independent sealed bootstrap helper may differ from
Source and candidate; verify its actual immutable graph and stable record rather
than requiring it to be a serving runtime. Source/candidate pointer identity stays
exact. No unknown process is stopped or adopted, no original maintenance releases.

Admin audit and `cancelled_unchanged`/separate-slot closure commit atomically. The
original operation, slot, fencing, plan, deadlines and failure/dispatch history
remain. Retain full metadata proof only in the distinct native namespace, and
store its digest plus a safe typed summary in the audit; keep audit redaction
unchanged, hash request/idempotency identity, and support terminal response replay
without a live process-local sample. No automatic new intent or renewed budget
follows. Another budget requires an explicit administrator decision (FR-032).

### Fixed Darwin kernel identity inspection

The retained native acceptance reproduced a kernel-inspection timeout after full
runtime verification in a telemetry-enabled process, before command output was
read. Read the fixed `kern.bootsessionuuid` with `sysctlbyname` from the fixed
Darwin system library instead of spawning a subprocess from the threaded owner.
The read has a fixed 37-byte UUID/NUL buffer, no new value, no caller name or
path, strict UUID validation and the unchanged one-second monotonic bound capped
by the existing intent deadline. Check both clocks after the read. An error,
malformed/oversize value or overdue result remains a refusal. Launchctl inspection
and private child cleanup retain their existing bounds. This adds no dependency.

### Cold crash continuation at the selected Source boundary

Acceptance found a supported cold owner can die after actual Source launch and
before archive when fixture transports are unavailable. Repairing those ordinary
connections grants no ownership, and the expired root cannot be replayed with a
new PID or epoch. Implement a distinct explicitly approved cold continuation:
fixed installed observer obtains actual exclusion, positively proves predecessor
retirement and exact live retained Source identity, verifies unchanged original
history/native preimages, canonical physical owner cleanup and both data baselines.
Its metadata read budget is not effect authority. Initially support only the
Source-launched/before-archive boundary; refuse all other stages explicitly.

Acceptance subsequently reached completed archival and fresh serving, then
retained a failed pair result. Extend this boundary only when completed archive
intent/effect/receipt, original bytes, optional completed smoke and failed-pair
command/results agree with retained Core authority. Bind their digests flatly in
the new grant; require current canonical inventories to be entirely idle. Transfer
the protected archive namespace once with write-ahead evidence and unchanged
bytes. Preserve historical gaps and failed results, and require fresh serving and
new paired command UUIDs under the new budget. An unacknowledged archive, smoke or
pair dispatch is uncertain and refuses; committed verification and handback/closure
stages remain outside this extension. Completed qualification and an uncommitted
verification request are supported as historical metadata only, as detailed below. Bounded private launcher diagnostics retain actual
output for the scoped operator; API responses and logs expose only fixed failure
summaries, byte counts and digests. Diagnostics grant no dispatch or cleanup
authority and cannot make a previously dispatched command repeatable.

Admin acceptance captures a new bounded intent once, serializes the predecessor
and original operation, preserves original maintenance/active slot/deadlines,
and atomically transfers only the separate recovery slot. Retain predecessor
intent/claim/selection/launch/cancellation bytes and append an audited truthful
supersession receipt; never label it recovered or cancelled unchanged. Use a
separate immutable succession grant with a flat prior-native binding, avoiding
recursive proof growth. The current separate-recovery phase constraint needs an
exactly guarded additive terminal `superseded` state; do not change the old
operation phase constraint or claim original success. Live schema preparation
must be an authenticated audited typed operation on the known existing recovery
schema, with no arbitrary SQL, history rewrite or obligation release.

New native owner must obtain every actual lifetime/recovery/fixture flock and
recheck live Core authority. Only an exact positively verified retained Source
child may cross this boundary; journal explicit retention rather than claiming a
fresh launch. Preserve prior selection time and PID birth. Recheck runtime bytes,
Core/native baselines and exact scope history; actual smoke, archive, pair and
handback under the newly captured intent remain mandatory. Unknown or later
boundaries refuse with fencing. No observer, timer, crash handler or retry may
create a new recovery budget; prepare/test immutable code before requesting it.


Post-qualification crash continuation (T073): preserve completed old paired
results and their qualification plus an uncommitted verification request as
hash-bound historical metadata. Authenticated Core context must prove that no
verification/handback/resolution was committed. A new explicitly approved intent
must perform fresh Source smoke and three new exact-pair commands; historical
qualification grants no new authority. Unresolved dispatches and committed
verification/handback boundaries continue to refuse. Keep old canonical payload
hashes by excluding absent additive fields. The accepted old result bytes remain
unchanged. Verify tampering, pending-request provenance, and new command identity.


Bounded verifier cost (T073): every graph boundary still walks every path and
verifies links, modes, ownership and file metadata. For already byte-verified
regular files, compare complete kernel metadata (device/inode/size/mtime/ctime/
uid/gid/mode/link count) before and after cache lookup without reopening unchanged
content. Cache misses retain O_NOFOLLOW byte reads and recheck complete pre-open
identity. This preserves the canonical graph digest and cannot reuse cached bytes
after a same-size edit with restored mtime, a path replacement or metadata change.
Measure actual installed cost before consuming another bounded intent.


Post-handback continuation (T073): the measured closure failure occurred after
Source guard handback and local release; Core and peer fences remain held. Support
only an exact expired/cancelled predecessor with committed Source verification
and handback, immutable supervision/closure intent, positively retired support
owner and supervisor, and the recorded live idle Source guard/node. Bind released
native claim and local fence preimages without resetting their timestamps or
original deadlines. A new immutable grant may retire only that verified idle
Source guard through a durable authenticated transfer step, acquire actual native
lifetime/recovery/fixture exclusion and retain the live Source node. No original
claim/fence is reactivated. Fresh serving, three new pair UUIDs, data preservation
and verified supervision remain mandatory. Final Source handback preserves any
already released claim exactly. Local closure records a fresh exact maintenance
observation separately from the immutable release-effect timestamp; freshness of
an observation never renews or fabricates an effect. Cover lost responses, crashes,
foreign guard/claim/fence, busy inventories, partial retirement and expiry before
native use. Keep old payload hashes unchanged by excluding absent additive fields.


Legacy post-release inventory boundary (T073): installed preflight proved the
sealed Source rejects strict inventory under released maintenance (HTTP409).
Do not synthesize idle inventory or reactivate that epoch. Late metadata may
explicitly carry no local inventory, only with committed handback, released
preimages and a positively verified live idle Source writer/node. This is
preparation evidence, never physical qualification. Atomic admin acceptance
reserves a distinct local recovery maintenance epoch from the existing global
generation sequence, bound to the new recovery ID, actor and captured deadline.
The new native owner retains the prior released fence bytes as immutable history,
installs only this separately audited fence under its live grant and actual
recovery/fixture exclusion, and obtains real authenticated Source inventory
before any archive, smoke or pair command. Local snapshots carry the new fence
identity; peer snapshots keep the original held epoch. All contracts validate
these actual identities. Closure releases only the new local recovery fence after
fresh verification and Source supervision. The old claim, release receipt and
original deadlines remain unchanged; cancellation/crash retains the new fence.


Unstarted continuation lineage (T073): the installed startup barrier must allow
only the positively verified recorded late Source guard as well as its node;
unknown processes still block. A failed pre-owner startup cannot strand a
superseded physical Source owner. Permit metadata traversal of at most sixteen
immutable supersession links only when every grant binds that exact historical
owner and Source scope, and the current separate root is expired, audited
cancelled, active and has no issued ownership/smoke/verification/handback effects.
An installed observer must positively retire both recorded launcher and support
child and verify its private namespace contains no native effect markers. New
acceptance atomically supersedes only that unstarted root, preserves the original
physical owner's earlier supersession unchanged, and binds the unstarted intent
and process evidence in the new grant. No previous budget or owner is revived.


Recovery-fence continuation after Source projection (T073): the legacy readiness
flag intentionally refuses a support helper when the original claim is released.
During distinct recovery, require authenticated Source runtime/release identity,
positive retained node identity, exact current support guard status and the actual
recovery/native writer lease instead. Never change the legacy flag or claim.
An expired owner after projection may transfer custody of its independently held
local recovery fence without changing that fence ID, generation or deadline.
Bind the inherited fence and preserve the original released claim. If archived
history has not yet moved to that owner, bind its actual earlier custodian and
archive digest through immutable private predecessor links; transfer bytes once
under new ownership, with fresh physical inventory and no copied qualification.
Refuse missing/conflicting custody, unknown processes, changed fence, archive
tampering and lost/partial effects. Budget capture remains a distinct admin action.


Bounded closure serialization (T073): installed closure exceeded the fixed512KiB
private journal limit because it serialized the full verified handback twice,
once directly and once inside supervision. Keep that limit. Add typed compact
supervision evidence containing exact handback and full supervision receipt
digests, process, kind, definition and captured epochs. Closure still carries
one full verified handback; native producers must compare the compact evidence
to the exact retained full receipt before physical supervision checks and Core
resolution. Preserve old full payload parsing/hashes. Cover near-limit round trips,
tampered digests, supervisor identity, release freshness and every replay boundary.
Post-handback metadata binds the actual independently scoped local fence when
its identity differs from the original released claim; Core compares that scope
to the predecessor intent, not the original epoch.


Terminal host-hold reconciliation (T073): after actual recovered resolution,
Core/peer/native fences and both slots are released, but the independent shared
deployment operation hold remains. Add an authenticated audited idempotent admin
reconciliation pinned to the exact committed resolution digest. Under the normal
operation/admission/deployment exclusions, verify terminal original event/plan,
released Core fences and exact host hold identity; release only that hold through
the existing host-hold producer. Lost effects/audits replay the same scope. No
new execution budget, Source claim, receipt or deadline changes. Unknown holds
or another deployment owner refuse. Dedicated coordination storage is writable
for this ordinary API concern; secret/prepared/data mounts stay read-only.

### Unpublished preparation cancellation discovered in acceptance

A recovered legacy Source can retain a released claim whose original native
receipt is recovery-required. New acceptance must validate that predecessor
before creating a preparation namespace. For already retained unpublished
preparations, a human-admin API request authorizes cancellation of the exact
accepted plan with zero activation receipts and no consumed node command. A
node-authenticated read verifies the durable audited request. Immutable support
may archive only the strict prepared-only namespace under the native control
lock, with positive Source identity and unchanged selection, no accepted claim,
no activation/disk hold/receipt/workload effects. The ordinary unstarted closure
then provides actual Source-preserving cancellation evidence. This path neither
publishes a native claim nor extends a deadline nor launches a workload. Unknown
or partially effected namespaces remain fenced. Archive bytes remain available
for audit and exact lost-response replay.

### Legacy native acknowledgement after committed recovery

A frozen older Source reader requires a positive native terminal receipt before
accepting its next upgrade. Once canonical after-deadline resolution, peer/Core
release and host-hold reconciliation have completed, an explicit audited admin
request may authorize a native compatibility acknowledgement of that same
resolution. Immutable support verifies the exact retained resolution, current
Source process/selection and supervision, released scopes and unchanged native
intent. It appends hash-linked verification/rollback acknowledgements identifying
the recovery intent and canonical resolution digest. These reference the actual
serving evidence already obtained inside that separate recovery budget; they
run no probe, create no qualification and renew no deadline. Existing native
receipt bytes and all original failure/deadline records remain unchanged. The
new records explicitly identify recovery after the original deadline and never
original upgrade success. Publication must survive loss after either append.

If reboot has renumbered a staging device, ordinary disk release continues to
refuse. Terminal acknowledgement may instead quarantine the private reservation
without deleting bytes: require the recorded inode, size, allocation, owner and
mode, a boot newer than the held record, and a changed device; persist a typed
intent with both identities before an atomic move on the same current filesystem.
The original hold identity remains unchanged and is retired only after the
quarantined file is verified. Replay refuses missing, duplicated or changed files.
Quarantine retains actual disk usage and emits a retained-byte gauge. This is
scoped terminal cleanup under the existing audited grant, not permission to
release unknown workers or delete an ambiguously identified file.

The SC-005 isolated 100-stream gate uses a controlled Linux engine double with
explicit long-stream timing and a separate 100-slot fixture capacity. Baseline
saturation tests retain four slots and production per-engine admission defaults
remain unchanged. Gateway and node transport pools must support the admitted
100 connections concurrently; bound both pools at 128 instead of silently
serializing through 32 sockets. Idle keepalive capacity remains eight. Native
model qualification is still verified independently on Apple Silicon.
