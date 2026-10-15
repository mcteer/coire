# Feature Specification: Control-Plane Failover and Frontend Election

> Implementation scope amendment (2026-10-14): the owner requested Cloudflare deployment last.
> Complete and qualify authenticated origin/local/Studio behavior now; provider configuration
> and public-edge acceptance are deferred to that rollout. All identity, fencing, resource and
> resident-inference requirements remain in force. Public-edge timing is not established by
> origin-only testing.

**Feature Branch**: `feat/020-control-plane-failover`

**Roadmap ID**: 016 · control-plane failover (added 2026-08-29, not in the original roadmap)

**Created**: 2026-08-29

**Status**: Reconciled 2026-10-14 after feature 019; implementation exists, corrective work and release acceptance remain open

**Input**: User description: "Some sort of poller on all three hosts where someone else gets elected to host the frontend in OrbStack if the mini goes down. Mini = primary, edge-a = secondary, edge-b = tertiary, with degrading capabilities depending on how many healthy members there are."

## Overview

Today core is a single point of failure for everything a human touches: the frontend, the gateway, and the system of record all live there. If core dies, both Studios keep holding 256 GB of loaded models that nobody can reach. This feature adds a small election among the three hosts so that when core is unavailable, a Studio stands up a **stateless, inference-only** frontend and gateway against the models already resident on the cluster — and stands it down again when core returns.

The deliberate limit is that failover restores **access to inference**, not the control plane. Postgres stays on core and is never promoted, so a degraded frontend has no conversation history, no admin interface, and no credential management. That limit is what keeps the feature compatible with the constitution's rule that nothing on a Studio may be a single source of truth.

## Constitutional Alignment

Constitution v2.0.0 retains the 2026-09-05 amendment authorising this limited Studio-side
role: one declared Studio may host a stateless, inference-only failover frontend behind the
existing authenticated edge when core is unavailable. It prohibits a database, scheduler, MCP,
admin or mutation surface, model/engine lifecycle, and every source-of-truth role. Activation is
quorum-gated (or an audited break-glass action), and the tier hands back to core on recovery.

This feature implements that constitutional exception without extending it into a replicated
control plane. Security and mutation audit evidence remains mandatory under Principles IV and VI;
only conversation content, feedback, and billing/usage accounting are unavailable during failover.
The bounded election journal is safety and audit evidence, not a second system of record.

## Clarifications

### Session 2026-08-29

- Q: What may a promoted Studio actually run? → A: A stateless frontend and gateway only, serving inference against models already resident on the cluster. No Postgres, no scheduler, no MCP, no admin surface, no ops harness, no observability backends. Promoting the database would make a Studio a source of truth, which Principle II forbids for good reason — a Studio reboot must never lose platform state.
- Q: What state does a degraded frontend have to work from, if Postgres is on core? → A: A read-only snapshot replicated to each Studio while core is healthy, containing only what serving needs: the published model roster with capability profiles, and credential verification material. It is a cache, never authoritative, is refused if older than a staleness bound, and is never written to. Conversation content, usage accounting, and feedback are not persisted. Minimal grant safety and security/election audit evidence are retained separately.
- Q: How is split-brain prevented with three hosts? → A: Quorum. A host may promote itself only while it can see a majority of declared hosts, meaning at least two of three agreeing that core is unreachable. Without the separately audited FR-020 break-glass exception, a host in a minority partition MUST NOT promote and MUST stop serving if already promoted, because two frontends answering for one cluster is worse than none.
- Q: What triggers and reverses promotion? → A: The same health signals feature 009 defines — heartbeat liveness with damping, and the degraded-versus-unreachable distinction. Promotion requires core to be `unreachable`, not merely `degraded`, sustained past a failover threshold. Demotion happens when core returns and is confirmed healthy for longer than the promotion threshold, so a flapping core cannot oscillate the cluster.
- Q: Does the promoted node keep serving models, given the frontend now costs it memory? → A: Yes, and the cost is reserved. The emergency tier has a standing reservation in that node's ledger, held from the moment this feature ships so its arrival does not change admission behaviour, exactly as the agent-sandbox slice does.

### Session 2026-10-14

- Q: Which existing feature is being prepared? → A: The user selected 020 control-plane failover; run specify, clarify, plan, tasks, and analysis only, then hand off for a model switch.
- Assumption: Preserve the current 120-second snapshot freshness limit and fail closed after expiry, including during break-glass. This release offers bounded outage access; extending offline authority requires a separate product/security decision.
- Assumption: Retain the existing sharded-inference requirement because the user has not authorised its deferral. The implementation must prove every resident rank and its data link are healthy; current single-Studio support is not evidence of sharded acceptance. API keys, visual inputs, adapters and tool/agent execution remain unavailable.
- Assumption: “Stateless” excludes conversation and usage persistence, but permits minimal restart safety records and bounded, content-free security/election evidence required by Principles IV and VI. Unacknowledged evidence cannot be silently dropped.
- Assumption: Normal hand-back uses the existing 30-second drain deadline; loss of authority fences immediately. Break-glass is host-bound, expiring, replay-protected, and requires the operator to fence other public origins before activation.
- Assumption: Preserve 019 ownership, 022 control/data separation, and 023 lean observability. Use existing timing defaults with explicit 60/90-second end-to-end targets and measured resource budgets; unmet targets remain open acceptance work.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The cluster keeps serving inference when core dies (Priority: P1)

Core goes down. Within a bounded interval a Studio promotes itself, serves a minimal chat surface and an inference endpoint against resident models, and users can keep working.

**Why this priority**: This is the feature. Two Studios holding loaded models that nobody can reach is the failure this exists to remove.

**Independent Test**: Power off core, wait past the failover threshold, and complete a chat request through the promoted node.

**Acceptance Scenarios**:

1. **Given** a healthy cluster with models resident, **When** core becomes unreachable and quorum agrees, **Then** `coire-edge-a` promotes itself within the configured failover threshold.
2. **Given** a promoted node, **When** a user connects, **Then** a minimal chat surface is served and inference against resident models succeeds.
3. **Given** a promoted node, **When** a user inspects the interface, **Then** it clearly states that the platform is in degraded mode and names what is unavailable.
4. **Given** a promoted node, **When** a request needs a model that is not resident, **Then** it is refused with a clear reason rather than attempting an acquisition.
5. **Given** degraded mode, **When** a user sends messages, **Then** conversation content, feedback, and usage accounting are not persisted, and the interface discloses that limitation before submission; security and operational evidence is still retained.

---

### User Story 2 - Priority order is honoured (Priority: P1)

Promotion follows the declared order — core primary, `coire-edge-a` secondary, `coire-edge-b` tertiary — and only one host is ever promoted.

**Why this priority**: A deterministic order is what makes the failure mode predictable, and predictability is the stated design value throughout this platform.

**Independent Test**: Fail core and confirm edge-a promotes. Then fail edge-a as well and confirm
edge-b remains unpromoted without quorum; an audited break-glass action may then promote it.

**Acceptance Scenarios**:

1. **Given** core unreachable and both Studios healthy, **When** promotion occurs, **Then** `coire-edge-a` is promoted and `coire-edge-b` is not.
2. **Given** core and `coire-edge-a` both unreachable, **When** `coire-edge-b` has no quorum,
   **Then** it MUST NOT promote automatically; only an audited break-glass action may promote it.
3. **Given** any moment in the cluster's life, **When** promotion state is inspected, **Then** at most one host is promoted.
4. **Given** `coire-edge-a` promoted, **When** it becomes unreachable and edge-b still has quorum,
   **Then** edge-b may promote only if core remains ineligible and two valid grants exist; otherwise it fails closed. A healthy eligible core reclaims priority.

---

### User Story 3 - Split-brain is impossible (Priority: P1)

A network partition never produces two hosts each believing they are the frontend.

**Why this priority**: Two frontends answering for one cluster produce divergent behaviour that is harder to diagnose and recover from than a clean outage. This is the property that makes automatic failover safe rather than dangerous.

**Independent Test**: Partition a Studio from the other two hosts and confirm it neither promotes nor keeps serving if already promoted.

**Acceptance Scenarios**:

1. **Given** a host that can see fewer than a majority of declared hosts, **When** it evaluates automatic promotion, **Then** it MUST NOT promote; only FR-020 permits an independently fenced override.
2. **Given** a promoted host that loses quorum, **When** it detects the loss, **Then** it demotes and stops serving within the configured interval.
3. **Given** a partition that isolates core while core is still running, **When** the majority side promotes, **Then** core detects it is in the minority and stops serving the frontend.
4. **Given** any partition, **When** it heals, **Then** exactly one host remains or becomes promoted and the others are demoted.

---

### User Story 4 - Capability degrades explicitly with membership (Priority: P2)

The platform states which tier it is operating in, and what that tier can and cannot do.

**Why this priority**: A degraded platform that does not say it is degraded produces support burden and mistrust. It ranks below the mechanism only because the mechanism must exist first.

**Independent Test**: Bring the cluster through each tier and confirm the advertised capability matches what actually works.

**Acceptance Scenarios**:

1. **Given** all three hosts healthy, **When** the tier is reported, **Then** it is full service with the complete control plane on core.
2. **Given** core down and both Studios healthy, **When** the tier is reported, **Then** it is degraded-inference: chat and inference against resident models, using text-only base-model instances on either reachable Studio, including already-resident sharded instances only while every rank and the data link are proved healthy; no conversation persistence, admin, acquisition, training, images, visual inputs or adapters.
3. **Given** core down and one Studio down, **When** the tier is reported, **Then** automatic service is unavailable; with a valid fenced break-glass override it is minimal: single-node inference against base models resident on the survivor only.
4. **Given** any tier below full, **When** a user or operator asks, **Then** the unavailable capabilities are enumerated explicitly rather than failing individually on use.

---

### User Story 5 - Core reclaims its role cleanly (Priority: P2)

Core comes back and the cluster returns to full service without an operator intervening and without oscillating.

**Why this priority**: A failover that requires manual recovery is only half a feature, and one that flaps is worse than none.

**Independent Test**: Restore core, confirm demotion after the confirmation window, and confirm no oscillation when core flaps.

**Acceptance Scenarios**:

1. **Given** a promoted Studio and core returning, **When** core is confirmed healthy beyond the demotion threshold, **Then** the Studio demotes and core resumes full service.
2. **Given** demotion, **When** it completes, **Then** the emergency frontend is stopped and the standing ledger reservation remains available for the next failover.
3. **Given** core flapping repeatedly, **When** health is evaluated, **Then** damping prevents repeated promotion and demotion.
4. **Given** a completed failover and recovery, **When** core is back, **Then** the event, its duration, and the tier reached are recorded for later audit.

---

### Edge Cases

- Core is `degraded` but not `unreachable`: promotion MUST NOT occur, since a slow core still holds authoritative state and a second frontend would diverge from it.
- The replicated read-only snapshot on a Studio is older than its staleness bound: election may continue, but serving readiness and all inference MUST fail closed; an expired snapshot cannot authorise models or identities and cannot be renewed by a Studio or a break-glass action.
- A user authenticates during degraded mode: verification uses the cached material if fresh; if not, access MUST be refused rather than granted on unverifiable credentials.
- Core returns while a request is in flight on the promoted node: new requests MUST stop immediately, and existing requests may complete only within the bounded drain window and while their authority remains valid; deadline expiry or lost authority terminates them with a truthful interrupted result.
- Both Studios are promoted due to a bug: the quorum check MUST cause all but one to demote on the next evaluation, and the condition MUST alert.
- The promoted node is also running a large sharded rank: the emergency tier's reservation MUST already be accounted for, and promotion MUST NOT evict a running instance.
- Public ingress health is stale or ambiguous: it MUST fail closed rather than route a request to an
  unelected Studio.
- All three hosts are unreachable from each other: no host may promote automatically, since none has quorum. Break-glass requires independent fencing of the other public origins before service starts.
- An operator wants to force or prevent failover: a manual override MUST exist and MUST be audited.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Each of the three declared hosts MUST run a lightweight poller participating in health exchange and promotion decisions.
- **FR-002**: The poller MUST reuse feature 009's health signals — heartbeat liveness, the degraded-versus-unreachable distinction, and transition damping — rather than defining a second health model.
- **FR-003**: Promotion priority MUST be a declared static order: `coire-core` primary, `coire-edge-a` secondary, `coire-edge-b` tertiary.
- **FR-004**: A host MUST promote only when the higher-priority hosts are `unreachable`, not merely `degraded`, sustained beyond a configured failover threshold.
- **FR-005**: Automatic promotion MUST require two current, authenticated grants from distinct declared hosts for one membership epoch, term, and candidate; reachability alone is insufficient. The sole exception is FR-020.
- **FR-006**: A host serving under quorum authority that loses quorum MUST demote and stop serving within a configured interval. A valid FR-020 break-glass authority is the explicit exception and still obeys every expiry/fencing condition.
- **FR-007**: At most one host may admit new inference requests at any time. Grants MUST NOT overlap for different candidates across terms, restarts, or membership changes. A detected conflict MUST fence unsafe serving and alert.
- **FR-008**: A promoted Studio MUST run only a stateless frontend and inference gateway. It MUST NOT run a database, scheduler, MCP server, admin surface, ops harness, or observability backend.
- **FR-009**: The system MUST NOT promote or replicate the system of record to a Studio; Postgres remains on core exclusively.
- **FR-010**: A signed, read-only snapshot containing the published model roster, capability profiles,
  credential verification material, and election peer keys MUST be replicated atomically to each
  Studio while core is healthy.
- **FR-011**: The snapshot MUST be treated as a non-authoritative cache, MUST carry its replication
  time and expiry, and MUST be rejected for credential verification when stale, expired, or the
  signature is invalid.
- **FR-012**: Degraded mode MUST NOT persist prompts, responses, conversations, feedback, or billing/usage records. It MUST disclose this before submission. Bounded security, override, and election audit evidence MUST be retained without prompt or response content (FR-021).
- **FR-013**: Degraded mode MUST serve only published, unrestricted, resident text base models in single-Studio or fully healthy already-resident sharded placements (FR-031). Adapters, caller-selected exact variants, vision, tool/agent execution, acquisition, conversion, training, and image generation MUST be explicitly refused; model identity MUST never fall back to a different target.
- **FR-014**: The system MUST report its current tier — full, degraded-inference, or minimal — and MUST enumerate unavailable capabilities explicitly rather than failing them individually on use.
- **FR-015**: The emergency frontend MUST have a standing reservation in each Studio's memory ledger, held from this feature's delivery, so its arrival does not change admission behaviour.
- **FR-016**: Promotion MUST NOT evict a running model instance.
- **FR-017**: Demotion MUST begin after core is continuously healthy for a confirmation window longer than promotion. Admission stops before drain; requests may finish within the configured drain deadline only while their authority remains valid. Lost authority or deadline expiry MUST cancel remaining work and report interruption. Core may admit new work only after the predecessor is fenced.
- **FR-018**: Demotion MUST stop the emergency frontend after drain. The standing ledger reservation MUST remain held for the next failover.
- **FR-019**: Public ingress MUST use separate outbound-only tunnels for core and each Studio plus
  an authenticated-edge load balancer that probes election-gated readiness; it MUST route only to
  the elected serving tier and fail closed when none is eligible.
- **FR-020**: An authenticated human administrator MUST be able to inhibit failover or issue an expiring break-glass promotion bound to exactly one host and membership epoch. Activation MUST require Core to be unavailable and independent fencing of the other public origins, reject replay and cross-host reuse, and record actor, reason, identity, expiry, and outcome before admission. A pre-authorised signed artifact may be delivered through the documented authenticated node path when core is down; signing secrets MUST remain in Keychain-backed operator custody. Break-glass never overrides snapshot expiry or identity checks.
- **FR-021**: Every promotion, demotion, quorum loss, tier change, override, and security event MUST retain content-free evidence and reconcile idempotently into core audit after recovery. Unacknowledged evidence MUST NOT be silently evicted. Journal corruption, exhaustion, or inability to persist required evidence MUST refuse further admission and administrative changes; safety fencing must still proceed and preserve a reserved terminal-event record.
- **FR-022**: The emergency frontend MUST fit its standing 256 MiB reservation, and incremental poller overhead MUST remain within the declared CPU and memory budgets measured against a baseline with failover disabled. Promotion MUST not introduce model eviction or swap growth.
- **FR-023**: The failover frontend MUST be a separate stateless service with only health/tier,
  model-listing, authenticated text inference routes, and their static UI assets. Every admin, mutation, MCP, image, run,
  scheduler, and acquisition route MUST be absent.
- **FR-024**: Inference targets MUST be authorised by the snapshot and proved currently resident by
  live node health. The service MUST never load, place, acquire, or create a request lease.
- **FR-025**: Studio peers MUST authenticate election and cross-Studio inference relay with
  separately scoped, Keychain-sourced credentials; node registration or broad node tokens MUST NOT
  be reused.
- **FR-026**: Every failover route MUST enforce its declared identity and scope: Access browser identity for user routes/assets, a dedicated authenticated monitor identity for readiness, and separately scoped signed peer authority for control exchanges and relay. A browser identity MUST also be explicitly eligible in the fresh signed snapshot; valid edge identity alone is insufficient. API keys, accounts requiring unavailable mutable budget accounting, and unverifiable identities MUST be refused; the frontend MUST not add an unauthenticated origin bypass.
- **FR-027**: Election state MUST remain safe across participant restart, missing/corrupt safety records, clock jumps, and membership replacement. A restarting voter MUST recover outstanding grants or wait out the maximum prior grant lifetime plus clock uncertainty before voting or serving. Unbounded clock uncertainty MUST fence serving.
- **FR-028**: Failover MUST respect feature 019 maintenance ownership. Core upgrade/rollback and Studio environment transitions MUST not fabricate election health, transfer writer ownership, trigger engine lifecycle actions, or release reservations. During a Core maintenance window, only independently authorised resident inference may remain available; hand-back additionally requires the authoritative Core cohort to be ready.
- **FR-029**: Baseline operation MUST emit local structured logs, traces, actionable metrics and alerts for election, fencing, snapshot age, audit backlog, resource budgets, and hand-back. Optional diagnostics storage MUST not be required for safety, audit retention, or alerts; disabled exporters MUST not retry absent destinations.

- **FR-030**: Degraded inference MUST enforce snapshot-approved prompt/context/output bounds and conservative per-identity rate/concurrency limits without creating billing records. Signed peer liveness MUST prove freshness so a replay cannot keep a failed member healthy.

- **FR-031**: Sharded failover MUST bind the signed snapshot to the exact resident instance, model target, group generation, rank identities and serving rank. Every rank and the Studio data link MUST have current authenticated health evidence before admission and throughout a request. Missing, failed or ambiguous rank/link evidence MUST refuse or interrupt the sharded request without substituting a single-node model, re-placing, restarting, or changing any engine. Minimal lone-survivor mode MUST refuse sharding.

### Key Entities

- **Cluster Member**: A declared participant in the election. Host, priority rank, health state, last heartbeat, quorum view, promotion state.
- **Promotion State**: Which host currently serves the frontend. Host, promoted-at, reason, quorum size at promotion, tier.
- **Service Tier**: The capability level in force. Name, healthy member set, available capabilities, unavailable capabilities.
- **Failover Snapshot**: The read-only cache on a Studio. Published model roster, capability profiles, credential verification material, replicated-at, staleness bound.
- **Election Authority**: A time-bounded quorum proof or one-host break-glass override, with durable minimal restart/replay guards.
- **Eligible Identity**: A core-approved Access subject and conservative limits valid only within the signed snapshot; no API-key secret or mutable billing state.
- **Resident Group**: Exact model/instance and generation with serving rank, peer rank identities and current authenticated rank/link evidence.
- **Failover Event**: An audit record. Type, host, trigger, tier before and after, duration, reconciled-at.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With core powered off and fresh identity/roster authority, public authenticated resident inference MUST recover within 60 seconds under the documented acceptance configuration, including detection, election, frontend startup, and public ingress propagation.
- **SC-002**: Promotion follows the declared priority order in 100% of trials, and at most one host admits new inference.
- **SC-003**: In 100% of induced partitions without a valid break-glass override, a minority host never automatically promotes and loses serving authority no later than the remaining 15-second lease plus one 2-second evaluation interval; the authenticated request path MUST reject an already expired proof immediately.
- **SC-004**: No database, scheduler, admin surface, or observability backend ever runs on a Studio, verified by inspection during failover.
- **SC-005**: No degraded prompt, response, conversation, feedback, or billing/usage record is persisted, verified after recovery; required content-free security and operational evidence is retained and reconciled exactly once.
- **SC-006**: The current tier and its unavailable capabilities are reported accurately at every tier.
- **SC-007**: A fully ready, authoritative Core cohort restores public full service automatically within 90 seconds of continuous healthy recovery, including the 45-second confirmation, up to 30-second drain, and measured ingress propagation.
- **SC-008**: Ten Core recovery pulses shorter than the 45-second confirmation window MUST cause zero hand-backs; stable recovery then causes exactly one hand-back.
- **SC-009**: Promotion never evicts a running model instance.
- **SC-010**: Every failover event is reconciled into the audit trail once core returns.
- **SC-011**: A lone surviving Studio never promotes automatically in 100% of induced tests; an
  audited, independently fenced break-glass promotion is the only permitted override.
- **SC-012**: Across restart, overlapping-term, replay, membership-change, and clock-jump fault trials, zero requests are admitted by conflicting owners or expired authority.
- **SC-013**: With the default 120-second snapshot freshness bound, stale/invalid snapshots and unverified or ineligible identities yield zero successful admissions, including during break-glass.
- **SC-014**: Over a 15-minute idle observation, incremental participant overhead is at most 32 MiB RSS and 1% of one CPU core on average; the frontend stays within 256 MiB, and promotion/coexistence produces zero model evictions and zero actual swap growth. These are acceptance budgets, not claimed measurements.
- **SC-015**: A Core upgrade success, failed-candidate rollback, and Studio environment transition preserve 019 writer ownership and data while producing no conflicting ingress owner or unmanaged engine action.
- **SC-016**: Missing route credentials, replayed/wrong-host overrides, journal exhaustion/corruption, and missing diagnostics storage each produce the specified refusal, fencing, audit, and alert outcome in the fault matrix.

- **SC-017**: Over-limit prompts, output requests, request rates, and concurrent requests MUST be refused before engine work; captured signed liveness exchanges MUST not make an unavailable member appear healthy.

- **SC-018**: With both Studios healthy and Core unavailable, an already-resident sharded base model serves through the elected frontend. Losing either rank or the data link prevents new sharded work and interrupts active work within one 2-second evaluation interval plus the 1-second health-probe timeout, with zero fallback or lifecycle actions; single-node inference and VLAN election remain available when their authority remains valid.

## Assumptions

- **Constitution v2.0.0 authorises this feature's exact Studio-side failover role.**
  The implementation remains limited to that role and cannot add a replicated control plane.
- Features 001–011 have shipped, in particular the health model from feature 009, the memory ledger from feature 004, instances from feature 005, and credentials from feature 007.
- OrbStack is already installed and running on both Studios (verified 2026-08-29), so the emergency tier needs no new runtime — only an image and a compose definition held ready.
- The Studios' inference-only footprint constraint applies: the poller and the failover frontend
  must be small, budgeted, and removable.
- Public ingress uses a Cloudflare Load Balancer with distinct, outbound-only tunnel endpoints for
  core, edge-a, and edge-b. The existing Access policy remains attached to the stable public
  hostname; individual tunnel routes are deny-by-default.
- Quorum over three hosts means two. This gives no protection if two hosts fail simultaneously,
  which is accepted: automatic promotion is unavailable to a lone survivor; a break-glass action
  is required and must be reconciled to audit when core returns.
- Degraded mode is deliberately not a full control plane. Extending it toward one would require replicating the system of record to a Studio, which Principle II forbids and which this design rejects.
- Per Principle VII, deterministic unit/contract/composed tests and tiny-model integration run in isolated CI; real-cluster power loss, ingress, partitions, coexistence, and recovery measurements remain mandatory release acceptance. CI never targets the real Studios.
- Existing defaults remain 2-second evaluation, 15-second promotion/lease, 45-second recovery confirmation, 30-second drain, and 120-second snapshot freshness. The 60/90-second end-to-end objectives include public ingress; incompatible external monitor settings block qualification instead of silently extending these objectives.
- Availability ends when cached authority expires. Extended offline authentication, offline revocation guarantees, API-key use, adapted serving, and indefinite outage service are outside this release.
- Features 019, 022, and 023 define upgrade ownership, VLAN control paths/Studio-only data fabric, and lean observability. Their invariants remain binding. Credentials, load-balancer entitlement, a pinned failover image, and registry-approved resident test assets are acceptance prerequisites, not reasons to defer this design.
- This revision is specification/planning only. Historical completed tasks record earlier delivery; they do not establish current release qualification. Open corrective tasks and measured acceptance must pass before enablement.
