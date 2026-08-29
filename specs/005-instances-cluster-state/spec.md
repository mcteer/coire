# Feature Specification: Model Instances and Cluster State

**Feature Branch**: `005-instances-cluster-state`

**Roadmap ID**: 003a (Phase 1 — Single-node inference works end to end)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "`ModelInstance` state machine (`requested → reserving → launching → warming → ready → draining → stopped|failed`) as a DBOS workflow; `/api/v1/instances` (`/state`) and per-instance SSE events (create + await); gateway routes to instances, multiple instances per model; declared node inventory with per-node registration tokens (no discovery)."

## Overview

Up to now "loading a model" has been an action. This feature makes it a durable object: a `ModelInstance` — a model variant placed on a specific set of nodes — with an explicit state machine, persisted and driven by a workflow so that a scheduler restart mid-launch resumes rather than orphans processes. It gives the cluster a `/state` surface, lets clients create an instance and await readiness over a stream, allows several instances of one model to coexist, and replaces any notion of discovery with a declared node inventory whose members authenticate with per-node registration tokens.

## Clarifications

### Session 2026-08-29

- Q: Why introduce instances when models already load? → A: Because routing and lifetime need to address a *placement*, not a model. One model may be resident twice — say a 4-bit copy on Studio A and a sharded copy across both — and the gateway must be able to pick between them, drain one, and keep the other. Making the instance the unit is also what lets a launch be resumable, since there is a row to resume against.
- Q: What does `draining` mean precisely? → A: The instance accepts no new requests but finishes those already admitted, then stops. Drain is bounded by a timeout, after which remaining requests are terminated and the instance stops anyway; an unbounded drain would let one stuck request pin a node's memory indefinitely.
- Q: How does the gateway choose among several ready instances of one model? → A: Prefer an instance already on the node holding the caller's affinity when one applies, then the least-loaded by in-flight count. Sharded and single-node instances of the same model are both eligible unless the request requires a capability only one provides.
- Q: What stops an unregistered machine on the VLAN from becoming a worker? → A: Nodes are rows created by an admin, each issued a registration token. A node agent presents its token on boot; an agent with no matching row or an invalid, revoked, or already-consumed token is refused and recorded. Nothing multicasts and nothing self-registers.
- Q: What happens to in-flight requests when an instance fails? → A: They receive an error immediately rather than waiting for a timeout, the instance moves to `failed`, and its reservation is released. Re-placement is a separate scheduler decision, not an automatic part of the failure path.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A launch survives a scheduler restart (Priority: P1)

An instance is mid-launch when the scheduler restarts. The workflow resumes and the instance still reaches `ready`, with no orphaned engine process left behind.

**Why this priority**: This is the roadmap's named acceptance bar and the entire reason instances are durable objects. An orphaned engine holding 200 GB that the ledger knows nothing about is the worst failure the cluster can have.

**Independent Test**: Start a launch, restart the scheduler mid-flight, and confirm the instance reaches `ready` with exactly one engine process.

**Acceptance Scenarios**:

1. **Given** an instance in `launching`, **When** the scheduler restarts, **Then** the workflow resumes and the instance reaches `ready`.
2. **Given** an instance in `reserving` when the scheduler restarts, **When** it resumes, **Then** the reservation is not double-counted in the ledger.
3. **Given** a resumed launch, **When** it completes, **Then** exactly one engine process exists for that instance and no orphan remains.
4. **Given** a launch that cannot resume because its node is gone, **When** resumption is attempted, **Then** the instance moves to `failed` with a reason and its reservation is released.

---

### User Story 2 - A client creates an instance and awaits readiness (Priority: P1)

A caller asks for a model to be made available, receives an instance identity immediately, and follows a stream of state events until it is ready.

**Why this priority**: This is the create-and-await pattern the gateway's wait behaviour is built on, and what the admin console will use to show live load progress.

**Independent Test**: Create an instance for a cold model, subscribe to its events, and observe the state sequence through to `ready`.

**Acceptance Scenarios**:

1. **Given** a `ready` model that is not loaded, **When** a caller creates an instance, **Then** an instance identity is returned immediately in `requested`.
2. **Given** that instance, **When** the caller subscribes to its events, **Then** it receives state transitions in order through to `ready`.
3. **Given** a launch that fails, **When** the caller is subscribed, **Then** it receives a terminal failure event with a reason rather than the stream simply ending.
4. **Given** a caller subscribing after the instance is already ready, **When** it subscribes, **Then** it receives the current state immediately rather than waiting for the next transition.

---

### User Story 3 - Cluster state is inspectable in one place (Priority: P1)

An operator reads a single view showing every node, every instance, their states, placements, and reservations.

**Why this priority**: This is the cluster's source of truth for both the admin console and every diagnostic conversation. Without it, "what is running right now" is answered by guesswork.

**Independent Test**: With instances running across both nodes, read cluster state and confirm it matches what the nodes actually have loaded.

**Acceptance Scenarios**:

1. **Given** instances on both nodes, **When** an operator reads cluster state, **Then** every node, its health state, live CPU and GPU utilisation, thermal state, budget and reservations, and every instance with its state and placement are returned.
2. **Given** an instance in each state, **When** state is read, **Then** each is reported accurately with the timestamp of its last transition.
3. **Given** a node that has gone unreachable, **When** state is read, **Then** the node is reported unreachable and its instances are reported as such rather than as ready.

---

### User Story 4 - Two instances of one model coexist (Priority: P2)

The same model runs as more than one instance — different variants or different placements — and the gateway routes across them.

**Why this priority**: This is what lets the ledger hold two big models usefully and what makes a sharded and a single-node copy of one model coexist. It is not required for the first request to work.

**Independent Test**: Create two instances of one model on different nodes and confirm both serve and the gateway distributes across them.

**Acceptance Scenarios**:

1. **Given** one model, **When** two instances are created on different nodes, **Then** both reach `ready` and both are independently addressable.
2. **Given** two ready instances, **When** requests arrive, **Then** the gateway distributes them by least in-flight count.
3. **Given** two ready instances, **When** one is drained, **Then** traffic moves to the other with no failed requests.

---

### User Story 5 - Only declared nodes may join (Priority: P2)

An admin declares a node and issues it a registration token. A machine without a valid token cannot become a worker, no matter where it sits on the network.

**Why this priority**: Principle IV and the architecture's explicit rejection of discovery. On an internet-exposed platform a rogue worker is a data-exfiltration path.

**Independent Test**: Attempt registration from an undeclared machine on the VLAN and confirm refusal and an audit record.

**Acceptance Scenarios**:

1. **Given** an admin-created node row and its token, **When** the node agent registers with that token, **Then** registration succeeds and the node becomes usable.
2. **Given** an undeclared machine on the lab VLAN, **When** it attempts to register, **Then** it is refused and the attempt is recorded.
3. **Given** a revoked token, **When** the node attempts to register, **Then** it is refused and the node is marked unreachable rather than being silently trusted.
4. **Given** any network, **When** the system runs, **Then** no discovery or multicast mechanism exists by which a peer could be found.

---

### Edge Cases

- Two callers request the same model simultaneously when none is loaded: exactly one instance MUST be created and both callers MUST await it.
- An instance is created for a model whose files are missing on the chosen node: it MUST fail during launch with a specific reason rather than warming forever.
- A drain exceeds its timeout: remaining requests MUST be terminated and the instance MUST stop rather than hanging in `draining`.
- An engine dies while an instance is `ready`: the instance MUST move to `failed`, in-flight requests MUST receive errors promptly, and the reservation MUST be released.
- A node returns after being unreachable while its instances were presumed lost: its actual processes MUST be reconciled against instance rows, adopting matches and reporting orphans.
- An instance is stopped while a client is subscribed: the subscriber MUST receive a terminal event.
- A registration token is used twice: the second use MUST be refused, since a token maps to exactly one node identity.
- The workflow crashes between reserving memory and launching: resumption MUST NOT double-reserve.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST model a `ModelInstance` as a persisted record of a model variant, a placement, the nodes and ranks it occupies, and its state.
- **FR-002**: Instance state MUST be one of `requested`, `reserving`, `launching`, `warming`, `ready`, `draining`, `stopped`, or `failed`, with every transition timestamped and reasoned.
- **FR-003**: An instance's lifecycle MUST be driven by a durable workflow that resumes after a control-plane restart rather than orphaning processes.
- **FR-004**: Resumption MUST NOT double-reserve memory or start a second engine for one instance.
- **FR-005**: The system MUST expose a cluster state view returning all nodes with reachability, live CPU and GPU utilisation, thermal state, budget, and reservations, and all instances with state and placement.
- **FR-005a**: Node reachability in cluster state MUST distinguish `healthy`, `degraded`, and `unreachable`, and MUST carry the observation time of the health data it reports.
- **FR-006**: The system MUST allow creating an instance and MUST return its identity immediately, before it is ready.
- **FR-007**: The system MUST expose a per-instance event stream carrying state transitions, and a late subscriber MUST receive the current state immediately.
- **FR-008**: A terminal failure MUST be delivered to subscribers as an explicit event, not as a closed stream.
- **FR-009**: The gateway MUST route requests to instances, never directly to models.
- **FR-010**: A model MUST support multiple concurrent instances, and the gateway MUST select among ready instances by least in-flight count, honouring affinity when applicable.
- **FR-011**: `draining` MUST accept no new requests, MUST allow admitted requests to finish, and MUST be bounded by a timeout after which remaining requests are terminated.
- **FR-012**: An engine failure MUST move its instance to `failed`, deliver errors to in-flight requests promptly, and release its reservation.
- **FR-013**: Concurrent requests for an unloaded model MUST result in exactly one instance being created, with all callers awaiting it.
- **FR-014**: Nodes MUST be declared as admin-created rows; no discovery, multicast, or self-registration mechanism may exist.
- **FR-015**: Each node MUST be issued a registration token, and an agent presenting an absent, invalid, revoked, or already-consumed token MUST be refused and the attempt recorded.
- **FR-016**: A node returning from unreachability MUST have its actual processes reconciled against instance rows, adopting matches and reporting orphans.
- **FR-017**: An unreachable node's instances MUST NOT be reported as ready.
- **FR-018**: Every instance creation, transition, and node registration MUST write an audit record.

### Key Entities

- **Model Instance**: A placement of a model variant. Model, variant, placement, nodes and ranks with ports and process identities, state, transition history, reservation reference, in-flight count.
- **Node**: A declared cluster member. Identity, DNS name, role, registration token reference, health state (`healthy`/`degraded`/`unreachable`), live CPU and GPU utilisation, thermal state, budget, last-heartbeat time, health observation time.
- **Instance Event**: A transition delivered to subscribers. Instance, previous and new state, reason, timestamp.
- **Registration**: A node's admission to the cluster. Node, token reference, presented-at, outcome, agent version.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A scheduler restart during a launch resumes and the instance reaches `ready`, in 100% of trials, with no orphaned engine.
- **SC-002**: Two instances of one model coexist on different nodes and both serve traffic.
- **SC-003**: Draining one of two instances moves traffic to the other with zero failed requests.
- **SC-004**: An unregistered machine on the lab VLAN cannot join the cluster, in 100% of attempts, and every attempt is recorded.
- **SC-005**: Cluster state matches what the nodes actually have running, verified by independent inspection of node processes.
- **SC-006**: Concurrent requests for one unloaded model produce exactly one instance.
- **SC-007**: An engine killed while ready produces prompt errors to in-flight callers and a released reservation, with no request waiting for a timeout.
- **SC-008**: A drain never exceeds its configured timeout before the instance stops.

## Assumptions

- Features 001–004 have shipped: the registry, node agent verbs, the gateway, and the memory ledger with eviction and idle TTL exist.
- Feature 004's placement decisions now produce instances rather than bare loads; eviction operates on instances and drains them rather than killing engines outright.
- Sharded instances span two nodes with ranks; the state machine accommodates them here, but launching them is feature 006.
- Durable workflows execute on the control plane and drive node work through the node agent's API; no workflow worker runs on a Studio.
- Registration tokens are per-node and issued by an admin. Full credential lifecycle, rotation, and scoping are feature 007; this feature needs only issue, present, and revoke.
- The node inventory replaces feature 000's static per-node token arrangement, closing that feature's time-boxed exception to Principle IV.
- Integration tests exercise the state machine with tiny models; multi-node behaviour is verified manually on the real cluster before merge.
