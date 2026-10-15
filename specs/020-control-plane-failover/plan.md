# Implementation Plan: Control-Plane Failover and Frontend Election

**Branch**: `feat/020-control-plane-failover` | **Date**: 2026-10-14 | **Spec**: [spec.md](spec.md)

## Summary

Qualify the existing stateless inference tier against the reconciled specification after feature
019 merged as `cd355554`. Preserve the delivered service and historical task evidence. Complete
identified authority, audit, ingress, recovery, and upgrade-coexistence gaps before live enablement.
This planning pass changes documents only; it does not claim current code meets the revised gates.

Core remains the sole system of record. Core's API and both node agents participate in a fixed
three-member election. One digest-pinned Studio container may serve bounded, authenticated resident
text inference, including existing sharded instances with every rank proved healthy. Signed snapshots expire; expiration ends degraded availability rather than extending
cached authority. Scope and exclusions are defined in FR-013, with any user clarification recorded
in the spec before tasks are finalised.

## Technical Context

**Language/Version**: Existing Python 3.13 workspace and strict TypeScript/React frontend.

**Primary Dependencies**: Existing FastAPI, Pydantic v2, HTTPX, Ed25519 support, Cloudflare Access,
Tunnel and Load Balancing. No new package is planned. Preserve exact lockfile pins; any necessary
addition requires licence/rationale evidence in the PR.

**Storage**: Postgres and authoritative audit remain on core. Studios retain signed public snapshots,
minimal grant/restart safety metadata, and bounded content-free audit journals. They never persist
conversation data or become the source of truth for users, models, jobs, or billing.

**Testing**: Existing unit/contract suites, deterministic multi-participant fault tests, composed
service tests, isolated tiny-model integration, web tests, generated-schema checks, hardened image
policy/scan/SBOM, and separately recorded real-cluster power/partition/public-ingress trials.

**Target Platform**: Existing macOS/OrbStack hosts, VLAN DNS control listeners, Studio-only
Thunderbolt data fabric. No listener, firewall, CORS, or capability expansion is assumed.

**Project Type**: Existing distributed control plane with a separate stateless ASGI/frontend image.

**Performance Goals**: Public failover within 60 s; public hand-back within 90 s. Existing defaults:
2 s evaluation, 15 s promotion/lease, 45 s recovery confirmation, 30 s maximum drain, 120 s snapshot
freshness. Frontend <=256 MiB; participant incremental idle RSS <=32 MiB and CPU <=1% of one core
averaged over 15 minutes. Measure actual swap growth, not Darwin filesystem pageouts.

**Constraints**: Quorum proof or fenced, host-bound break-glass authority; fresh snapshot and Access
identity; no new model lifecycle or user harness on core; no mutable control plane on a Studio.
The failover frontend itself starts no engine. Node-owned engines remain on Studios as usual.
Diagnostics storage is optional; security audit and actionable alerts are mandatory.

**Scale/Scope**: Three declared members; priority core, edge-a, edge-b; one admission owner. Resource
and timing values are qualification targets, not measurements. Longer offline authority is deferred.

## Constitution Check

| Principle | Design gate | Required evidence |
|---|---|---|
| I. Bare engines | PASS | Relay only to proven node-owned resident engines; no load/place/acquisition calls. |
| II. Core/workers | PASS | Exactly the Constitution v2.0.0 inference-only exception; all authoritative state stays on core. |
| II-a. Bare service image | PASS | Existing dedicated non-root/read-only/drop-all image, digest pin, healthcheck, minimal networks. |
| III. Contracts first | PASS | Change strict coire-core models first; generate API/failover OpenAPI and both web schemas; assert generated node OpenAPI in contract tests. |
| IV. Zero implicit trust | PASS | Authenticate every route; bind override to human/host/epoch; audit intent before delivery; enforce admission at origins and relay. |
| V. Models as data | PASS | Snapshot-approved registry identity and live exact residency; refuse unsupported targets. |
| VI. Observable | PASS | Content-free audit retention, baseline metrics/alerts/logs, optional diagnostics with no dead exporters. |
| VII. Spec/test gates | PASS | Explicit corrective tasks plus measured isolated and live fault matrix; no skipped required acceptance. |

These are **design compliance** results. Existing implementation gaps below remain release blockers.
No constitutional exception or amendment is requested.

## Project Structure

### Documentation

`specs/020-control-plane-failover/` contains spec, requirements checklist, this plan, research,
data model, contract design, quickstart, historical review, tasks, and model-switch handoff.

### Existing source and planned edits

```text
packages/coire-core/src/coire_core/models/failover.py
packages/coire-core/src/coire_core/{failover_election,failover_poller,failover_snapshot,failover_telemetry}.py
packages/coire-core/src/coire_core/settings.py
apps/coire-api/src/coire_api/failover/{participant,poller,publication,snapshot,overrides,reconcile}.py
apps/coire-api/src/coire_api/routes/{failover,admin_failover}.py
apps/coire-api/src/coire_api/upgrades/inventory.py
apps/coire-node/src/coire_node/failover/{participant,poller,journal,auth,relay}.py
apps/coire-node/src/coire_node/routes/failover.py
apps/coire-failover/src/coire_failover/{app,auth,resolution,proxy,tier}.py
apps/coire-web/src/{api/failover.ts,pages/Failover.tsx}
deploy/{compose,cloudflared,observability}/
docs/runbooks/control-plane-failover.md
tests/integration/test_control_plane_failover.py
```

The concrete election implementation lives in coire-core, not a new failover/election.py module.
Add narrowly scoped safety helpers/tests only where existing modules cannot keep the logic clear.

## Scope preservation

The original sharded-failover requirement remains in scope (FR-031); its removal was offered as a
clarification but has not been authorised. Add exact instance/group generation, rank membership,
serving-rank identity and authenticated live rank/link evidence to the snapshot/residency contracts.
Use the existing node sharding status and relay paths to observe resident groups; failover cannot
start, stop, re-place, load or repair ranks. Refuse ambiguous groups. Recheck all ranks/link while
streaming, interrupt on loss within one evaluation interval plus a 1 s probe timeout, and preserve
unaffected single-node inference. Any longer timeout requires an explicit spec change. Test rank
failure and Thunderbolt-only loss both in isolation and on the real cluster; no silent single-node
fallback is permitted. A lone survivor never serves a sharded instance.

## Phase 0: Research and baseline reconciliation

See [research.md](research.md). Read current code against the spec; historical checkmarks do not
prove restart-safe grants, authenticated probes, non-overlapping ingress, or bounded stream drain.
Inventory each existing public/control route and map it to a declared credential. Identify external
load-balancer prerequisites read-only before attempting a live trial. Do not install dependencies
or alter live ingress as part of this planning pass.

## Phase 1: Design

### Authority and restart safety

Keep the existing two-grant protocol, but bind every proof/override to the fixed membership epoch,
intended holder, term/identity, and bounded lifetime. Validate exactly one authority variant.
Persist the minimal outstanding grant/term guard atomically before issuing a grant; use a boot-time
quarantine for missing or corrupt records, lasting at least the maximum previously permitted lease
plus clock uncertainty. Configuration changes cannot shorten an outstanding guard. Preserve trusted
epoch/snapshot rollback bounds across restart. Never renew expired snapshots locally.

Use monotonic elapsed time for local deadlines and an explicit bounded wall-clock uncertainty for
cross-host expiry; the acceptance bound is 1 s. The fault model covers crashes, partitions and
replay, not compromised participant signing keys. Compare signed challenge timestamps and bounded
round-trip uncertainty with the trusted healthy baseline; a peer assertion alone cannot reset an
out-of-bound local clock or shorten a durable outstanding-grant guard. Detect backward/forward discontinuities and fence until a
fresh authenticated synchronization/lease establishes bounded authority. Voters may not grant another
candidate while any previous grant remains potentially live. A release implementation must prove
these properties with an independently scheduled multi-participant/restart test, not just call-order
unit tests. This safety record is not replicated application/control-plane state.

### Admission, identities, and ingress

Every request is fenced at the serving application and at node relay, even if the external load
balancer routes to a stale pool. Core normal inference admission must check both election ownership
and feature 019 maintenance authority. Health of the process alone does not authorize serving.

Separate browser Access identity, monitor credential, local lifecycle/status credential, signed
peer election messages, and relay credential. Add strict coire-core models for acknowledgements,
monitor/status responses where needed, request-bound relay authority and cancellation, override
identity/fencing evidence, and event acknowledgement. Preserve `/v1` compatibility and additive
`coire_` fields. The design contract lists intended changes; production OpenAPI remains generated.

Access JWKS retrieval uses signed verifier metadata and fails closed on failed validation; there is
no offline identity fallback. Only unrestricted published models are available; no stale entitlement
assertion or API-key impersonation is accepted. Bound bodies, prompt/context/output limits, request
rate, and concurrent inference using the approved snapshot limits and local conservative counters;
no billing ledger is introduced on a Studio. Initial fallback admission ceilings are one in-flight
request and 10 requests/minute per Access subject, 1,024 output tokens, and the smaller of the
model context window or 4,096 prompt tokens; snapshot policy may tighten these values. There is no
account-level budget bypass: users/keys requiring mutable budget accounting are ineligible for this
Access-only tier. Verify the policy before enabling the feature.

Authenticated readiness is non-cacheable and returns 503 unless all serving prerequisites hold.
Dedicated monitor credentials may authenticate the health probe; they never authorize inference or
assets. Node lifecycle gets a separate scoped way to read in-flight status. All origins enforce
Access for browser assets and routes, and missing direct-origin credentials fail closed.

Treat Cloudflare health as routing advice rather than ownership authority. Measure actual monitor
propagation and all-unhealthy fallback behavior. If an external fallback still forwards, the origin
must refuse. Do not widen an Access policy to make a monitor pass. Any new network/policy exception
requires the user's separate approval under AGENTS.md; code and design work can continue meanwhile.

### Break-glass and audit

An override includes a unique ID, kind, actor, single target, epoch, issue/expiry, reason, signature,
and recorded independent fencing evidence. It has one delivery target; inhibit scope is explicit.
Authenticate a human administrator, match actor to the principal, audit intent before delivery, then
audit delivery/activation outcome. Offline delivery uses a previously authorised artifact and a
Keychain-backed operator path, never a new Studio signing or admin service. Reject replay,
wrong-host/epoch/kind, stale/future authority, and simultaneous proof plus override.

Core must be unavailable, and the user must independently fence other public origins before break-glass activation. This is an
operational precondition of overriding quorum, not a claim that one isolated Studio can prove the
others are dead. The runbook defines verification and rollback evidence. No override extends the
snapshot freshness window.

Use durable event IDs and authenticated acknowledgements. Retain unacknowledged entries; compact
only acknowledged entries. Reserve terminal-fence capacity outside the ordinary event budget, and
refuse new admission/overrides before it is exhausted. Corruption or write failure fences safely;
recovery requires retaining evidence and re-establishing the journal. Include actor/reason/override
identity and lifecycle transitions without prompts, tokens, keys, or response content.

### Drain, recovery, and upgrades

Stop new requests before drain. Track real in-flight streams through an authenticated local status
surface; cancellation/disconnect decrements accounting reliably. Revalidate lease, snapshot and
request identity expiry while streaming; lost authority terminates promptly. A still-authorised
request may finish up to the 30 s drain deadline. Successor admission waits for the predecessor's
fence acknowledgement or conservative expiry, not just a readiness probe.

Keep the 256 MiB standing reservation after demotion. Exercise 019 Core upgrade success, failure and
rollback plus a Studio environment transition using existing owner inventory/admission. Failover
never drives an upgrade, steals a writer claim, or starts/stops engines to maintain inference.
Core cannot reclaim public service until its authoritative cohort is ready; epoch/version mismatch
fails closed. No changes to 019 database roles or handover protocol are planned. Database-backed publication and
audit reconciliation may return a typed maintenance refusal while liveness/vote safety exchanges
continue; refusing a guarded write must not mark a member unreachable.

### Capability and observability

Report election availability separately from capability tier: a lone unpromoted Studio is an outage,
not working minimal service. Show snapshot expiry and no-history disclosure before submission.
Return stable refusal reasons for unsupported inputs/targets; preserve an unsent draft during a
transient refusal without claiming the conversation is stored. Accessibility follows existing
keyboard/live-region conventions.

Extend existing metrics/spans/logs, dashboard and alert tests for journal pressure, expired authority,
resource overruns and hand-back. Use 023 baseline collector configuration; dashboards/history may
require diagnostics but election, audit, and alerts do not. Never buffer prompt content in telemetry.

## Validation and release sequence

1. Complete schemas and regression tests, then services, then web. Update only tests needed for
   changed behavior, while preserving the existing required CI gates.
2. Run relevant unit/contract/web/schema checks once per meaningful change, then required composed
   and isolated tiny-model cases. A test failure justifies focused reruns; no blanket repeated loop.
3. Build/scan the changed production images and verify topology and healthchecks. Record exact
   tested commit/image digests, commands, results and any concrete missing prerequisites.
4. Run the [quickstart](quickstart.md) real-cluster fault matrix from a healthy, freshly signed state
   per scenario; retain independent timing, residency, origin, audit and 019 ownership observations.
5. Close T036 only after all corrective tasks and required acceptance pass, update roadmap/runbook,
   prepare a PR with Principles I–VII evidence, and leave enablement explicit and reversible.

## Post-Design Constitution Re-check

PASS at design level: no new authoritative Studio service, database, engine wrapper, acquisition
path, or network allowance. Minimal safety/audit metadata supports fencing without becoming the
platform system of record. Every external contract change has a coire-core-first task. Existing
source is not declared compliant until the remaining task evidence demonstrates it.
