# Research: Placement Scheduler and Auto-Unload

## R1 — Serialize admission with PostgreSQL advisory locks

**Decision**: Acquire a transaction-scoped advisory lock derived from the node UUID before reading
or mutating that node's ledger. Re-read all reservations under the lock.

**Rationale**: It prevents two DBOS workflows from admitting against the same freed bytes without a
new dependency or a permanently held distributed lock.

**Alternatives considered**: Serializable transactions (broader retry surface); process locks (do
not survive replicas/restarts); Redis locks (new stateful service).

## R2 — Reservations are the admission authority

**Decision**: Persist standing sandbox and model-engine reservations separately from sampled RSS.
Measured resident bytes are diagnostic drift only.

**Rationale**: Deterministic decisions survive load spikes and match FR-002 and the constitution.

**Alternatives considered**: `psutil` free memory (moves during admission); node-reported free bytes
(stale and non-serializable).

## R3 — Durable placement remains scheduler-owned

**Decision**: A DBOS workflow owns select → optional evict → reserve → load. Node verbs cross the
existing API-side authenticated command executor; the scheduler never receives node tokens or a
Studio network attachment.

**Rationale**: Preserves Principles II-a and IV while making restart recovery deterministic.

**Alternatives considered**: direct scheduler-to-node calls (widens token/network scope); API-only
policy (loses DBOS durability and scheduler ownership).

## R4 — Request leases close the TTL/traffic race

**Decision**: The gateway creates an expiring request lease and increments in-flight count before
proxying, refreshes `last_used_at`, then releases in `finally`. TTL and eviction check leases under
the same node lock; stale leases expire after a bounded recovery window.

**Rationale**: A process crash cannot leave a model busy forever and an admitted request cannot be
unloaded beneath itself.

**Alternatives considered**: in-memory counters (lost on restart); last-used timestamp alone (race).

## R5 — LRU eligibility and bounded drain

**Decision**: Sort ready engine reservations by `last_used_at`, excluding pinned entries and active
leases. When only busy unpinned entries could make room, poll for a configurable bounded drain
window, then return a typed refusal listing each occupant and its reason.

**Rationale**: Implements deterministic LRU without evicting work in flight or waiting forever.

## R6 — Health and drift

**Decision**: Admission requires a healthy, fresh node sample; unreachable reservations remain
held. Expose `(measured resident - model reservations) / model reservations` as diagnostic drift and
alert at absolute value >10%.

**Rationale**: Stale data blocks rather than guesses; accounting remains conservative.

## R7 — Dependencies and licences

**Decision**: Add no dependency. Use SQLAlchemy/PostgreSQL, DBOS, and OTel already pinned and
licensed by prior features.

**Rationale**: The existing stack supplies transactions, durable workflows, and telemetry.
