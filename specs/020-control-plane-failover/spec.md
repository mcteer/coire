# Feature Specification: Control-Plane Failover and Frontend Election

**Feature Branch**: `020-control-plane-failover`

**Roadmap ID**: 016 · control-plane failover (added 2026-08-29, not in the original roadmap)

**Created**: 2026-08-29

**Status**: Draft — **blocked on a governance decision, see Constitutional Conflict below**

**Input**: User description: "Some sort of poller on all three hosts where someone else gets elected to host the frontend in OrbStack if the mini goes down. Mini = primary, edge-a = secondary, edge-b = tertiary, with degrading capabilities depending on how many healthy members there are."

## Overview

Today core is a single point of failure for everything a human touches: the frontend, the gateway, and the system of record all live there. If core dies, both Studios keep holding 256 GB of loaded models that nobody can reach. This feature adds a small election among the three hosts so that when core is unavailable, a Studio stands up a **stateless, inference-only** frontend and gateway against the models already resident on the cluster — and stands it down again when core returns.

The deliberate limit is that failover restores **access to inference**, not the control plane. Postgres stays on core and is never promoted, so a degraded frontend has no history, no admin, no audit, and no credential management. That limit is what keeps the feature compatible with the constitution's rule that nothing on a Studio may be a single source of truth.

## Constitutional Conflict — must be resolved before planning

This feature as requested conflicts with the ratified constitution and **cannot proceed until an amendment or a recorded exception exists**:

- **Principle II** states the Studios run the node agent, model/training/image processes and agent sandboxes — *"nothing else: no databases, no web tier."* Electing a Studio to host the frontend is a web tier on a Studio.
- **Principle II-a** places every control-plane service in one compose project on core.
- **Principle II** also states *"Nothing on a Studio may be a single source of truth"* — with which the stateless design below is compatible, and which a Postgres failover would violate outright.

Two governance routes, per the constitution's own Governance section:

1. **Amend Principle II** (MINOR version bump) to permit a stateless, inference-only emergency tier on a Studio, explicitly excluding databases and any durable state. Recommended — it makes the exception a stated rule rather than a standing violation.
2. **Record a time-boxed ADR exception** under `docs/adr/`, if failover is to be treated as provisional.

The rest of this specification is written assuming route 1.

## Clarifications

### Session 2026-08-29

- Q: What may a promoted Studio actually run? → A: A stateless frontend and gateway only, serving inference against models already resident on the cluster. No Postgres, no scheduler, no MCP, no admin surface, no ops harness, no observability backends. Promoting the database would make a Studio a source of truth, which Principle II forbids for good reason — a Studio reboot must never lose platform state.
- Q: What state does a degraded frontend have to work from, if Postgres is on core? → A: A read-only snapshot replicated to each Studio while core is healthy, containing only what serving needs: the published model roster with capability profiles, and credential verification material. It is a cache, never authoritative, is refused if older than a staleness bound, and is never written to. Anything a user does in degraded mode — conversations, usage, feedback — is not persisted.
- Q: How is split-brain prevented with three hosts? → A: Quorum. A host may promote itself only while it can see a majority of declared hosts, meaning at least two of three agreeing that core is unreachable. A host that finds itself in a minority partition MUST NOT promote and MUST stop serving if already promoted, because two frontends answering for one cluster is worse than none.
- Q: What triggers and reverses promotion? → A: The same health signals feature 009 defines — heartbeat liveness with damping, and the degraded-versus-unreachable distinction. Promotion requires core to be `unreachable`, not merely `degraded`, sustained past a failover threshold. Demotion happens when core returns and is confirmed healthy for longer than the promotion threshold, so a flapping core cannot oscillate the cluster.
- Q: Does the promoted node keep serving models, given the frontend now costs it memory? → A: Yes, and the cost is reserved. The emergency tier has a standing reservation in that node's ledger, held from the moment this feature ships so its arrival does not change admission behaviour, exactly as the agent-sandbox slice does.

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
5. **Given** degraded mode, **When** a user sends messages, **Then** nothing is persisted and the interface says so before they invest in a conversation.

---

### User Story 2 - Priority order is honoured (Priority: P1)

Promotion follows the declared order — core primary, `coire-edge-a` secondary, `coire-edge-b` tertiary — and only one host is ever promoted.

**Why this priority**: A deterministic order is what makes the failure mode predictable, and predictability is the stated design value throughout this platform.

**Independent Test**: Fail core and confirm edge-a promotes; then fail edge-a and confirm edge-b takes over.

**Acceptance Scenarios**:

1. **Given** core unreachable and both Studios healthy, **When** promotion occurs, **Then** `coire-edge-a` is promoted and `coire-edge-b` is not.
2. **Given** core and `coire-edge-a` both unreachable, **When** promotion occurs, **Then** `coire-edge-b` is promoted.
3. **Given** any moment in the cluster's life, **When** promotion state is inspected, **Then** at most one host is promoted.
4. **Given** `coire-edge-a` promoted, **When** it becomes unreachable, **Then** `coire-edge-b` promotes within the failover threshold.

---

### User Story 3 - Split-brain is impossible (Priority: P1)

A network partition never produces two hosts each believing they are the frontend.

**Why this priority**: Two frontends answering for one cluster produce divergent behaviour that is harder to diagnose and recover from than a clean outage. This is the property that makes automatic failover safe rather than dangerous.

**Independent Test**: Partition a Studio from the other two hosts and confirm it neither promotes nor keeps serving if already promoted.

**Acceptance Scenarios**:

1. **Given** a host that can see fewer than a majority of declared hosts, **When** it evaluates promotion, **Then** it MUST NOT promote.
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
2. **Given** core down and both Studios healthy, **When** the tier is reported, **Then** it is degraded-inference: chat and inference against resident models, including sharded instances if the link is healthy; no persistence, no admin, no acquisition, no training, no images.
3. **Given** core down and one Studio down, **When** the tier is reported, **Then** it is minimal: single-node inference against models resident on the survivor only, no sharding.
4. **Given** any tier below full, **When** a user or operator asks, **Then** the unavailable capabilities are enumerated explicitly rather than failing individually on use.

---

### User Story 5 - Core reclaims its role cleanly (Priority: P2)

Core comes back and the cluster returns to full service without an operator intervening and without oscillating.

**Why this priority**: A failover that requires manual recovery is only half a feature, and one that flaps is worse than none.

**Independent Test**: Restore core, confirm demotion after the confirmation window, and confirm no oscillation when core flaps.

**Acceptance Scenarios**:

1. **Given** a promoted Studio and core returning, **When** core is confirmed healthy beyond the demotion threshold, **Then** the Studio demotes and core resumes full service.
2. **Given** demotion, **When** it completes, **Then** the emergency frontend is stopped and its ledger reservation is released for model use.
3. **Given** core flapping repeatedly, **When** health is evaluated, **Then** damping prevents repeated promotion and demotion.
4. **Given** a completed failover and recovery, **When** core is back, **Then** the event, its duration, and the tier reached are recorded for later audit.

---

### Edge Cases

- Core is `degraded` but not `unreachable`: promotion MUST NOT occur, since a slow core still holds authoritative state and a second frontend would diverge from it.
- The replicated read-only snapshot on a Studio is older than its staleness bound: promotion MUST still occur for inference, but the node MUST refuse credential verification it cannot trust and MUST state which capabilities are unavailable for that reason.
- A user authenticates during degraded mode: verification uses the cached material if fresh; if not, access MUST be refused rather than granted on unverifiable credentials.
- Core returns while a request is in flight on the promoted node: in-flight requests MUST complete before demotion stops the emergency frontend.
- Both Studios are promoted due to a bug: the quorum check MUST cause all but one to demote on the next evaluation, and the condition MUST alert.
- The promoted node is also running a large sharded rank: the emergency tier's reservation MUST already be accounted for, and promotion MUST NOT evict a running instance.
- Public ingress points at core: failover MUST redirect ingress to the promoted host, and if that cannot be automated the manual step MUST be documented in a runbook.
- All three hosts are unreachable from each other: no host may promote, since none has quorum.
- An operator wants to force or prevent failover: a manual override MUST exist and MUST be audited.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Each of the three declared hosts MUST run a lightweight poller participating in health exchange and promotion decisions.
- **FR-002**: The poller MUST reuse feature 009's health signals — heartbeat liveness, the degraded-versus-unreachable distinction, and transition damping — rather than defining a second health model.
- **FR-003**: Promotion priority MUST be a declared static order: `coire-core` primary, `coire-edge-a` secondary, `coire-edge-b` tertiary.
- **FR-004**: A host MUST promote only when the higher-priority hosts are `unreachable`, not merely `degraded`, sustained beyond a configured failover threshold.
- **FR-005**: A host MUST promote only while it can see a majority of declared hosts.
- **FR-006**: A promoted host that loses quorum MUST demote and stop serving within a configured interval.
- **FR-007**: At most one host may be promoted at any time; the system MUST converge to one on the next evaluation if this is ever violated, and MUST alert.
- **FR-008**: A promoted Studio MUST run only a stateless frontend and inference gateway. It MUST NOT run a database, scheduler, MCP server, admin surface, ops harness, or observability backend.
- **FR-009**: The system MUST NOT promote or replicate the system of record to a Studio; Postgres remains on core exclusively.
- **FR-010**: A read-only snapshot containing the published model roster, capability profiles, and credential verification material MUST be replicated to each Studio while core is healthy.
- **FR-011**: The snapshot MUST be treated as a non-authoritative cache, MUST carry its replication time, and MUST be refused for credential verification when older than its staleness bound.
- **FR-012**: Degraded mode MUST NOT persist conversations, usage, feedback, or audit records, and MUST state this to the user before they invest in a conversation.
- **FR-013**: Degraded mode MUST serve inference only against models already resident on the cluster, and MUST refuse requests requiring acquisition, conversion, training, or image generation with a clear reason.
- **FR-014**: The system MUST report its current tier — full, degraded-inference, or minimal — and MUST enumerate unavailable capabilities explicitly rather than failing them individually on use.
- **FR-015**: The emergency frontend MUST have a standing reservation in each Studio's memory ledger, held from this feature's delivery, so its arrival does not change admission behaviour.
- **FR-016**: Promotion MUST NOT evict a running model instance.
- **FR-017**: Demotion MUST occur when core is confirmed healthy beyond a demotion threshold longer than the promotion threshold, and MUST allow in-flight requests to complete.
- **FR-018**: Demotion MUST stop the emergency frontend and release its ledger reservation.
- **FR-019**: Public ingress MUST be redirected to the promoted host on failover; where this cannot be automated, the manual procedure MUST be documented in a runbook.
- **FR-020**: An operator MUST be able to force or inhibit failover manually, and every such action MUST be audited.
- **FR-021**: Every promotion, demotion, quorum loss, and tier change MUST be recorded, and MUST be reconciled into the audit trail when core returns.
- **FR-022**: The poller's own resource use on a Studio MUST stay within a configured budget, consistent with the Studios' inference-only footprint.

### Key Entities

- **Cluster Member**: A declared participant in the election. Host, priority rank, health state, last heartbeat, quorum view, promotion state.
- **Promotion State**: Which host currently serves the frontend. Host, promoted-at, reason, quorum size at promotion, tier.
- **Service Tier**: The capability level in force. Name, healthy member set, available capabilities, unavailable capabilities.
- **Failover Snapshot**: The read-only cache on a Studio. Published model roster, capability profiles, credential verification material, replicated-at, staleness bound.
- **Failover Event**: An audit record. Type, host, trigger, tier before and after, duration, reconciled-at.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With core powered off, a Studio serves inference against resident models within the configured failover threshold.
- **SC-002**: Promotion follows the declared priority order in 100% of trials, and at most one host is ever promoted.
- **SC-003**: A host in a minority partition never promotes, and demotes if already promoted, in 100% of induced partitions.
- **SC-004**: No database, scheduler, admin surface, or observability backend ever runs on a Studio, verified by inspection during failover.
- **SC-005**: Degraded mode persists nothing, verified by inspecting state after core returns.
- **SC-006**: The current tier and its unavailable capabilities are reported accurately at every tier.
- **SC-007**: Core returning restores full service automatically, with no operator action.
- **SC-008**: A flapping core produces no repeated promotion and demotion.
- **SC-009**: Promotion never evicts a running model instance.
- **SC-010**: Every failover event is reconciled into the audit trail once core returns.

## Assumptions

- **This feature requires a constitutional amendment or a recorded ADR exception before planning may begin.** See Constitutional Conflict above. Route 1, amending Principle II to permit a stateless inference-only emergency tier, is recommended.
- Features 001–011 have shipped, in particular the health model from feature 009, the memory ledger from feature 004, instances from feature 005, and credentials from feature 007.
- OrbStack is already installed and running on both Studios (verified 2026-08-29), so the emergency tier needs no new runtime — only an image and a compose definition held ready.
- The Studios' inference-only footprint constraint applies: the poller and the emergency frontend must be small, budgeted, and removable.
- Public ingress runs through a tunnel terminating at core. Whether ingress can be repointed automatically depends on that tunnel's capabilities and is the main open question in this design; if it cannot, failover restores LAN access and the public path requires the documented manual step.
- Quorum over three hosts means two. This gives no protection if two hosts fail simultaneously, which is accepted: with three nodes there is no configuration that survives that and still avoids split-brain.
- Degraded mode is deliberately not a full control plane. Extending it toward one would require replicating the system of record to a Studio, which Principle II forbids and which this design rejects.
- Per Principle VII this feature requires documented manual verification on the real cluster before merge; failover cannot be meaningfully exercised in CI.
