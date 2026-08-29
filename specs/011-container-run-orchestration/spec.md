# Feature Specification: Container Run Orchestration on the Studios

**Feature Branch**: `011-container-run-orchestration`

**Roadmap ID**: 009 (Phase 3 — Agents)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "OrbStack on each Studio with a fixed memory slice in the ledger; coire-node `POST /runs` API brokering create/start/stream/wait/collect/remove against the local Docker socket; DBOS workflow in coire-scheduler choosing the Studio (co-located with the run's model, else free slots), run tokens, limits, kill switch, restart-resume. Core runs no user harness."

## Overview

Feature 010 built a harness; this feature runs it safely. Agent runs execute as ephemeral containers on the Studios, brokered by the node agent against each Studio's local container runtime, chosen and driven by a durable workflow on core. It establishes the sandbox properties that make a public platform tolerable — a single network route, no egress, short-lived scoped credentials, hard limits, and a kill switch that works — and it enforces the constitutional rule that no user harness ever runs on core.

## Clarifications

### Session 2026-08-29

- Q: Why do user runs go on the Studios rather than core, when core is the orchestrator? → A: Principle II forbids core from running any harness but `ops`. The Studios have the memory to spare and the models are already there, so co-locating a run with the model it will mostly use keeps gateway-to-node traffic local. The cost is a fixed memory slice per Studio, which the ledger has reserved since feature 004 precisely so this lands without changing admission behaviour.
- Q: What network access does a run container get? → A: Exactly one route, to the gateway on core. No internet, no database, no node agent, no peer containers. A coding run that needs to clone a repository receives it through a workspace volume prepared for it, not by reaching the network itself.
- Q: How is a run's credential scoped? → A: A short-lived run token minted per run, scoped to that run's permitted models, tools, and spend, and invalidated the moment the run ends or is killed. Killing a run must stop it from calling models even if the node is slow to acknowledge the kill, which a long-lived credential could not guarantee.
- Q: How is the Studio chosen? → A: Prefer the Studio already holding the model the run will mostly use; otherwise the Studio with a free run slot. If neither has a free slot, the run queues rather than exceeding the concurrency cap.
- Q: What proves runs never land on core? → A: The roadmap's acceptance bar — a run request succeeds while core's container runtime holds no agent image at all. If runs could land on core, that condition would fail rather than pass.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A run executes in a sandbox on a Studio (Priority: P1)

A run is requested; the scheduler picks a Studio; the node agent creates, starts, streams, waits on, collects from, and removes a container; and the result comes back.

**Why this priority**: This is the execution primitive every agent-facing feature composes — the MCP tools, the chat UI's code mode, and every later agent capability.

**Independent Test**: Request a run, observe it execute on a Studio, and collect its result.

**Acceptance Scenarios**:

1. **Given** a run request, **When** the scheduler admits it, **Then** a container is created and started on a Studio and its identity is recorded.
2. **Given** a running container, **When** it produces output, **Then** logs stream to the log backend and are attributable to the run.
3. **Given** a completed run, **When** it finishes, **Then** its result is collected from the agreed location and the container is removed.
4. **Given** a completed run, **When** it is inspected, **Then** its Studio, duration, exit status, and resource usage are recorded.

---

### User Story 2 - No user harness runs on core, provably (Priority: P1)

A run succeeds even when core's container runtime has no agent image at all, demonstrating that core is not a possible host.

**Why this priority**: This is a constitutional guarantee and the roadmap's named acceptance bar. A subtle fallback to core would be invisible until it mattered.

**Independent Test**: Remove the agent image from core entirely, request a run, and confirm it succeeds.

**Acceptance Scenarios**:

1. **Given** core's container runtime holding no agent image, **When** a run is requested, **Then** it succeeds on a Studio.
2. **Given** any run request, **When** it is scheduled, **Then** the chosen host is always a Studio and never core.
3. **Given** core, **When** its running containers are inspected, **Then** no user-facing harness container is present.
4. **Given** the scheduler, **When** its container access is inspected, **Then** it can reach only the control plane's own containers through the socket proxy, never a Studio's runtime directly.

---

### User Story 3 - A run is confined and cannot reach what it should not (Priority: P1)

A run container has one network route, no ability to escalate, hard resource limits, and a wall-clock timeout.

**Why this priority**: The platform is internet-reachable and runs execute model-authored actions. Containment is the property that makes that acceptable.

**Independent Test**: From inside a run, attempt to reach the internet, the database, and the node agent, and confirm all fail while the gateway succeeds.

**Acceptance Scenarios**:

1. **Given** a run container, **When** it attempts to reach the internet, the database, the node agent, or a peer container, **Then** every attempt fails.
2. **Given** a run container, **When** it reaches the gateway, **Then** the request succeeds and is authenticated by its run token.
3. **Given** a run container, **When** it is inspected, **Then** it runs as non-root with a read-only root filesystem, dropped capabilities, no privilege escalation, and explicit memory and CPU limits.
4. **Given** a run exceeding its wall-clock timeout, **When** the timeout passes, **Then** it is terminated and recorded as timed out.
5. **Given** a run's workspace, **When** the run ends, **Then** the container and its writable layer are removed.

---

### User Story 4 - A run can always be stopped (Priority: P1)

An admin kills a run and it stops quickly, and its credential stops working immediately regardless of how promptly the node responds.

**Why this priority**: A stuck or misbehaving harness that keeps consuming models is the failure mode this platform most needs a hard answer to.

**Independent Test**: Start a long run, kill it from the admin interface, and confirm it stops and its token is refused.

**Acceptance Scenarios**:

1. **Given** a running run, **When** an admin kills it, **Then** the container stops within 5 seconds.
2. **Given** the same kill, **When** the run token is presented afterwards, **Then** the gateway refuses it, even if the node has not yet confirmed the stop.
3. **Given** a killed run, **When** it is inspected, **Then** it is recorded as killed with the acting admin and time.
4. **Given** a run whose node is unresponsive, **When** it is killed, **Then** the token is invalidated immediately and the container is reaped when the node returns.

---

### User Story 5 - Runs survive a control-plane restart (Priority: P2)

The scheduler is restarted mid-run and the run resumes rather than being orphaned or duplicated.

**Why this priority**: The roadmap's named bar, and an orphaned container holding a memory slice is a capacity leak. It ranks below containment only because containment failures are worse than accounting failures.

**Independent Test**: Kill the scheduler mid-run, restart it, and confirm the run completes exactly once.

**Acceptance Scenarios**:

1. **Given** a run in flight, **When** the scheduler is killed and restarted, **Then** the run resumes and completes.
2. **Given** that resumption, **When** it completes, **Then** the run executed exactly once and no duplicate container was created.
3. **Given** a run whose container died while the scheduler was down, **When** the scheduler returns, **Then** the run is recorded as failed rather than awaited indefinitely.
4. **Given** containers on a Studio with no corresponding run row, **When** reconciliation runs, **Then** they are reported and reaped.

---

### Edge Cases

- Both Studios are at their concurrency cap: the run MUST queue rather than exceeding the cap or landing on core.
- The chosen Studio becomes unreachable after admission but before start: the run MUST be re-placed or failed explicitly, never left pending forever.
- A run writes an unreadable or absent result: it MUST be recorded as a result-collection failure distinct from a run failure.
- A run produces an enormous volume of logs: log ingestion MUST be bounded so one run cannot exhaust the log backend.
- A run attempts to consume more memory than its limit: it MUST be terminated by the limit rather than pressuring the node's model memory.
- A run's model is evicted mid-run: the run's gateway calls MUST behave like any other caller, waiting or receiving a retry response.
- The container runtime on a Studio is down: run requests MUST fail with a clear reason and MUST NOT fall back to core.
- A run token outlives its run through a clock problem: expiry MUST be enforced server-side rather than trusted from the token alone.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Agent runs MUST execute as ephemeral containers on a Studio, and MUST NEVER execute on core.
- **FR-002**: The node agent MUST be the only process on a Studio that accesses that Studio's container runtime, and core MUST NOT hold a remote runtime socket or shell access to a Studio.
- **FR-003**: The node agent MUST expose create, start, log-stream, wait, collect, and remove operations for runs, authenticated to the control plane.
- **FR-004**: A durable workflow on the control plane MUST decide that a run happens and where, and MUST drive the node agent through the run lifecycle.
- **FR-005**: Studio selection MUST prefer the Studio holding the model the run will mostly use, then a Studio with a free run slot.
- **FR-006**: Each Studio MUST have a configurable concurrency cap, defaulting to three runs, and runs beyond it MUST queue.
- **FR-007**: Each Studio MUST have a fixed container memory slice reserved in the ledger, defaulting to 16 GB and configurable per node, including to zero.
- **FR-008**: Each run MUST receive a short-lived run token scoped to its permitted models, tools, and spend.
- **FR-009**: A run token MUST be invalidated immediately when the run ends or is killed, independently of node acknowledgement, and expiry MUST be enforced server-side.
- **FR-010**: A run container MUST have exactly one network route, to the gateway, with no access to the internet, the database, the node agent, or peer containers.
- **FR-011**: A run container MUST run as non-root with a read-only root filesystem, dropped capabilities, no privilege escalation, and explicit memory and CPU limits.
- **FR-012**: Every run MUST have a hard wall-clock timeout after which it is terminated and recorded as timed out.
- **FR-013**: Run logs MUST stream to the log backend attributed to the run, with ingestion bounded per run.
- **FR-014**: A run's result MUST be collected from an agreed location, and an absent or unreadable result MUST be recorded distinctly from a run failure.
- **FR-015**: A run's container and writable layer MUST be removed when the run ends.
- **FR-016**: An admin MUST be able to kill any run, and the container MUST stop within 5 seconds when its node is responsive.
- **FR-017**: A control-plane restart MUST resume in-flight runs exactly once, without duplicating containers.
- **FR-018**: Containers with no corresponding run row MUST be reported and reaped by reconciliation.
- **FR-019**: When no Studio can host a run, the request MUST fail or queue with a clear reason and MUST NOT fall back to core.
- **FR-020**: Every run creation, kill, and terminal outcome MUST be audited.

### Key Entities

- **Agent Run**: One sandboxed execution. Identity, profile, requester, chosen node, container identity, run token reference, limits, timeout, state, exit status, result reference, timings.
- **Run Token**: A short-lived scoped credential. Run, permitted models, permitted tools, spend ceiling, expiry, revoked-at.
- **Run Slot**: Concurrency accounting per node. Node, cap, in-use count, queued runs.
- **Sandbox Slice**: The standing container-memory reservation per node. Node, bytes, configurable, reflected in the ledger.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A run request succeeds while core's container runtime holds no agent image at all.
- **SC-002**: 100% of runs execute on a Studio; no run ever executes on core.
- **SC-003**: A kill from the admin interface stops the container within 5 seconds and invalidates the run token immediately.
- **SC-004**: From inside a run, the internet, the database, the node agent, and peer containers are all unreachable, while the gateway is reachable.
- **SC-005**: Killing and restarting the scheduler mid-run resumes the run and it completes exactly once.
- **SC-006**: An orphaned container with no run row is detected and reaped by reconciliation.
- **SC-007**: A run exceeding its memory limit is terminated by the limit without affecting resident models on that node.
- **SC-008**: Every run's Studio, duration, exit status, and resource usage are recorded.

## Assumptions

- Features 001–010 have shipped, in particular the ledger with its sandbox slice, the instance model, credentials and audit, and the agent harness itself.
- OrbStack 2.2.3 is already installed and running on both Studios with Docker 29.4.0 and a live local socket (verified 2026-08-29). This feature therefore does not install a container runtime; it configures the fixed memory ceiling and concurrency cap, and builds the brokering path on top of what is there.
- The sandbox memory slice has been reserved in the ledger since feature 004, so introducing real containers does not change admission behaviour.
- Workspace contents for coding runs are prepared by the platform and mounted; a run never fetches from the network itself.
- Run tokens reuse the credential machinery from feature 007 with a distinct short-lived, scoped class.
- The `ops` harness on core is explicitly out of scope here; it is long-lived, is not a user run, and is feature 012.
- Studio concurrency starts at three runs per node and is tuned from observed behaviour.
- Per Principle VII, this feature requires a documented manual verification on the real cluster before merge.
