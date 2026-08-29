# Feature Specification: Placement Scheduler and Auto-Unload

**Feature Branch**: `004-placement-scheduler`

**Roadmap ID**: 003 (Phase 1 — Single-node inference works end to end)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "Memory ledger per node, LRU eviction, pinning, idle-TTL control loop, `single:auto` and pinned placements."

## Overview

Features 001–003 can load a model and serve it, but nothing decides *which* model lives where or when it should go away. This feature adds the memory ledger — the authoritative account of what is reserved on each node — plus the policies that act on it: LRU eviction when a load does not fit, pinning that exempts a model from eviction, and an idle-TTL control loop that unloads models nobody is using. It is what makes the cluster's finite memory behave like a managed resource rather than a race.

## Clarifications

### Session 2026-08-29

- Q: What is the ledger's relationship to measured memory? → A: The ledger is the authority for admission decisions and is built from reservations, not measurements. The node agent separately reports measured resident memory, and the difference is recorded as drift. Admission never consults raw measurements, because a decision based on a number that moves under load is not reproducible; drift instead raises an alert above the constitutional 10% threshold.
- Q: What does the scheduler do when nothing can be evicted? → A: It refuses the load with a reason naming what occupies the node and why each occupant is ineligible — pinned, or in use. It does not queue indefinitely and does not evict a pinned model under any circumstance.
- Q: Which model is chosen for eviction? → A: Least recently used among unpinned, idle models on the target node. A model with in-flight requests is not idle and is not evicted; it is skipped and the next candidate considered. If the only candidates are busy, the load waits briefly for one to drain before refusing.
- Q: Does the agent-sandbox memory slice participate in the ledger? → A: Yes, as a standing reservation line item on each node — 16 GB initially — deducted from that node's model budget from the moment the ledger exists, even though the sandboxes themselves arrive in feature 011. Introducing it later would silently change every admission decision.
- Q: How is `single:auto` resolved between two Studios? → A: Prefer Studio A, since it holds the larger GPU and is the default home for the largest single-node model, but fall back to Studio B when A lacks budget and B has it. An explicitly pinned placement always wins over auto-selection.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A load that does not fit evicts the right model (Priority: P1)

An operator or a request triggers a load onto a node without enough free budget. The scheduler evicts the least-recently-used unpinned idle model, makes room, and completes the load.

**Why this priority**: Without eviction the cluster wedges the first time someone asks for a second large model, which is the normal case on a 256 GB node.

**Independent Test**: Fill a node with two models, request a third that requires evicting one, and confirm the LRU one is evicted and the new load succeeds.

**Acceptance Scenarios**:

1. **Given** a node whose budget is fully reserved by unpinned idle models, **When** a load requiring room arrives, **Then** the least-recently-used unpinned idle model is unloaded and the new model loads.
2. **Given** the same node where the LRU model has in-flight requests, **When** a load arrives, **Then** that model is skipped and the next-least-recently-used idle candidate is evicted instead.
3. **Given** a node whose only occupants are pinned, **When** a load arrives that does not fit, **Then** it is refused with a reason naming the pinned occupants, and nothing is evicted.
4. **Given** an eviction, **When** it completes, **Then** the ledger reflects the released reservation before the new reservation is admitted.

---

### User Story 2 - Pinned models are never evicted (Priority: P1)

The admin model on Studio B stays resident permanently, and no amount of memory pressure removes it.

**Why this priority**: The ops harness depends on the pinned admin model. If pressure can evict it, the platform loses its ability to explain itself exactly when something is wrong.

**Independent Test**: Pin a model, apply memory pressure sufficient to require eviction, and confirm the pinned model survives and the pressuring load is refused.

**Acceptance Scenarios**:

1. **Given** a pinned model, **When** any load requires its memory, **Then** it is never selected for eviction and the load is refused or placed elsewhere.
2. **Given** a pinned model, **When** its idle TTL would otherwise expire, **Then** it is not unloaded.
3. **Given** an admin unpinning a model, **When** the pin is removed, **Then** it becomes an ordinary eviction candidate immediately.

---

### User Story 3 - Idle models release memory on their own (Priority: P2)

Models nobody has used for their configured TTL are unloaded automatically, returning memory to the budget without an operator doing anything.

**Why this priority**: On a two-node cluster, memory left held by a forgotten model is capacity nobody can use. It is a strong second priority but the cluster still functions with manual unloads.

**Independent Test**: Load a model with a short TTL, leave it idle past the TTL, and confirm it unloads and its reservation is released.

**Acceptance Scenarios**:

1. **Given** a loaded unpinned model idle beyond its TTL, **When** the control loop next evaluates, **Then** it is unloaded and its reservation released.
2. **Given** a model receiving requests, **When** the TTL interval passes, **Then** its last-used stamp keeps it resident and it is not unloaded.
3. **Given** a model unloaded by TTL, **When** a request for it arrives later, **Then** it loads again through the normal path.
4. **Given** differing TTLs per model, **When** the loop runs, **Then** each model is evaluated against its own TTL, not a global one.

---

### User Story 4 - An operator can see and trust the ledger (Priority: P2)

An operator inspects each node's budget, what is reserved, by what, and how far the reservations have drifted from measured reality.

**Why this priority**: The ledger's accuracy is a named risk in the architecture; making it visible is what turns a suspected drift into a diagnosable one.

**Independent Test**: Load several models and confirm the reported ledger accounts for each, plus the sandbox slice, and reconciles against node-reported memory within tolerance.

**Acceptance Scenarios**:

1. **Given** models loaded across both nodes, **When** the operator inspects the ledger, **Then** each node shows budget, total reserved, free, and a line item per reservation including the agent-sandbox slice.
2. **Given** reservations and measured memory, **When** they differ, **Then** the drift is reported per node.
3. **Given** drift exceeding 10%, **When** it is detected, **Then** an alert condition is raised.

---

### Edge Cases

- A load and an eviction race for the same freed memory: admission MUST be serialised per node so two loads cannot both be admitted against one release.
- An eviction's unload fails: the freed reservation MUST NOT be credited, and the pending load MUST be refused rather than admitted against memory that was never released.
- A node goes unreachable holding reservations: its reservations MUST be treated as held, not free, until it returns or is explicitly marked degraded.
- The idle loop and an incoming request race: a model MUST NOT be unloaded out from under a request that has already been admitted to it.
- Every model on a node is busy and a load is waiting: the scheduler MUST wait a bounded interval for a drain, then refuse with a reason.
- A model's measured memory greatly exceeds its estimate: the load MUST still be admitted on its reservation, but the discrepancy MUST be recorded and alerted, and the estimate MUST be correctable.
- Reducing a node's budget below current reservations: the change MUST be accepted without forcibly evicting, and MUST prevent new admissions until reservations fall below the new budget.
- The sandbox slice is set to zero on a node: admissions MUST immediately reflect the reclaimed budget.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST maintain a per-node memory ledger recording budget, every reservation, and free capacity.
- **FR-002**: Admission decisions MUST be made against reservations, never against instantaneous measured memory.
- **FR-003**: The ledger MUST include a standing agent-sandbox reservation per node, configurable and defaulting to 16 GB.
- **FR-004**: The scheduler MUST never admit a load that would exceed a node's budget.
- **FR-005**: A load that does not fit MUST trigger eviction of the least-recently-used unpinned idle model on that node.
- **FR-006**: A model with in-flight requests MUST NOT be evicted; the scheduler MUST consider the next candidate.
- **FR-007**: Pinned models MUST never be evicted and MUST never be unloaded by idle TTL.
- **FR-008**: When no eviction candidate exists, the load MUST be refused with a reason naming the occupants and why each is ineligible.
- **FR-009**: The scheduler MUST wait a bounded interval for a busy candidate to drain before refusing.
- **FR-010**: Freed memory MUST be credited to the ledger only after an unload is confirmed.
- **FR-011**: Admission per node MUST be serialised so concurrent loads cannot both consume one release.
- **FR-012**: A control loop MUST evaluate each loaded model against its own idle TTL and unload those that exceed it.
- **FR-013**: Every proxied request MUST update its model's last-used stamp.
- **FR-014**: A model MUST NOT be unloaded while a request already admitted to it is in flight.
- **FR-015**: `single:auto` MUST prefer Studio A and fall back to Studio B when A lacks budget.
- **FR-016**: An explicit pinned placement MUST take precedence over automatic selection.
- **FR-017**: The system MUST record drift between reserved and measured memory per node and MUST raise an alert condition above 10%.
- **FR-018**: An unreachable node's reservations MUST be treated as held until it returns or is marked degraded.
- **FR-018a**: The scheduler MUST NOT admit new loads onto a node reported `degraded` or `unreachable`, and MUST state the health reason when refusing.
- **FR-018b**: The scheduler MUST NOT make an admission decision using node health data outside its freshness window; stale health MUST be treated as unknown and MUST block admission rather than be assumed good.
- **FR-018c**: Sustained CPU or thermal saturation on a node MUST be usable as a placement input, so the scheduler can prefer the healthier Studio for `single:auto`.
- **FR-019**: Reducing a node's budget below current reservations MUST be permitted and MUST block new admissions until reservations fall below it.
- **FR-020**: Every eviction, refusal, and TTL unload MUST be recorded with its reason.

### Key Entities

- **Memory Ledger**: Per-node accounting. Node, budget, reservations, total reserved, free, measured memory, drift, updated-at.
- **Reservation**: One claim on a node's memory. Node, holder (model instance, conversion job, training job, sandbox slice), bytes, pinned flag, created-at, last-used-at.
- **Placement Decision**: The outcome of an admission request. Requested model, chosen node, evictions performed, refusal reason, timings.
- **Eviction Event**: An audit trail entry. Evicted model, node, triggering load, LRU rank, candidates skipped and why.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A load that does not fit evicts the correct LRU unpinned idle model and then succeeds, in 100% of trials.
- **SC-002**: A pinned model is never evicted or TTL-unloaded under any tested memory pressure.
- **SC-003**: The scheduler never admits a load that would push a node into swap.
- **SC-004**: Idle models unload within one control-loop interval of their TTL expiring.
- **SC-005**: A model receiving traffic is never unloaded by the idle loop.
- **SC-006**: Ledger drift versus measured resident memory stays within 10% under steady state, and exceeding it raises an alert.
- **SC-007**: The ledger accounts for every reservation including the sandbox slice, reconciling to the node's budget exactly.
- **SC-008**: Concurrent competing loads on one node never both succeed against a single freed reservation.

## Assumptions

- Features 001–003 have shipped: models reach `ready`, the node agent loads and unloads engines and reports memory, and the gateway routes and waits for loads.
- The instance state machine is feature 005. Until then a model has at most one loaded engine per node, which is what makes simple LRU sufficient here.
- Sharded placements are feature 006. This feature handles single-node and pinned placements only; the both-node simultaneous reservation arrives with sharding.
- Agent sandboxes arrive in feature 011, but their memory slice is reserved from this feature onward so admission behaviour does not change when they land.
- Training and conversion jobs reserve through the same ledger; conversion already exists from feature 002 and is upgraded here from queue-if-not-fitting to evict-then-run.
- Node memory budget defaults to roughly 230 GB of each Studio's 256 GB, leaving headroom for macOS; it is configurable per node.
- Alerting delivery is feature 009; this feature defines the drift condition and exposes the metric.
- Node health states (`healthy`/`degraded`/`unreachable`), the heartbeat that produces them, and their freshness windows are defined in feature 009 and consumed here. Until 009 ships, reachability is the only health input available and stale-health blocking applies to it alone.
- Integration tests use tiny models so eviction can be exercised on a single Mac by setting a small artificial budget.
