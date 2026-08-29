# Feature Specification: Node Health Agent and Bounded Self-Healing

**Feature Branch**: `021-node-self-healing`

**Roadmap ID**: 017 · node self-healing (added 2026-08-29, not in the original roadmap)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "Expand that poller to be a lightweight health agent that runs in a container on each host. If it drops somehow from the cluster, the agent performs some troubleshooting on the system to determine whether it can find and apply a fix to get the node back into the cluster."

## Overview

A node that drops out of the cluster today requires a human with a terminal, which is exactly what this platform is trying to avoid — and exactly what is hardest at 2 a.m. This feature gives each host the ability to notice it has fallen out, diagnose why against a known set of causes, and apply a bounded, reversible fix to rejoin. What it deliberately does not do is grant a general-purpose repair capability: remediation is an allowlist of deterministic actions with a circuit breaker, and anything outside that list becomes a diagnosis escalated to a human rather than an experiment run against a 256 GB inference node.

## Why this is split from feature 020

The request was to expand feature 020's poller. This is a separate specification because the two have very different risk profiles and can ship independently. Feature 020 elects a frontend and takes no action on a host. This feature acquires the ability to change a host's state, which is a materially larger security and blast-radius question and should not further delay a failover capability that is already blocked on a constitutional amendment. The two share the health model from feature 009 and the quorum view from feature 020.

## Scope Boundary — the agent's entire mandate

**The supervisor exists to restore the node's membership in the cluster. Nothing else.**

A node is "online" when three things are true: its node agent is running, it is reachable
over the network, and it is registered and heartbeating to the control plane. Restoring
those three conditions is the whole mandate. Anything that does not directly restore one of
them is out of scope, however tempting or easy it looks.

This boundary is applied in two tiers:

| Tier | Scope | Authority |
|---|---|---|
| **A — membership** | Restart the node agent; re-establish network connectivity; re-register and resume heartbeating; reclaim allowlisted scratch and log paths *only when disk exhaustion is preventing the node agent from running* | Autonomous, within the circuit breaker |
| **B — capacity** | Restart the container runtime, and anything else that affects what the node can *do* rather than whether it is *present* | Diagnosed and escalated, or proposed for human confirmation — never autonomous |

Everything else is out of scope entirely, and two exclusions matter because they would
otherwise create a second authority over something another feature already owns:

- **Crashed engines are not this agent's problem.** An engine dying is an instance failure,
  owned by feature 005's state machine and feature 004's scheduler. The node is online
  throughout. A supervisor restarting engines would compete with the scheduler for the same
  decision.
- **Engine environment rollback is not this agent's problem.** Feature 019 already rolls back
  automatically on a failed smoke test. Duplicating it here would mean two components
  reverting the same symlink on different evidence.

The supervisor MUST NOT touch model weights, adapters, checkpoints, job directories, user
data, engine processes, scheduler state, placement decisions, operating-system settings, or
installed packages. It MUST NOT install, upgrade, or remove software. If restoring
membership appears to require any of these, that is an escalation, not an action.

## Clarifications

### Session 2026-08-29

- Q: Can the health agent live only in a container? → A: No, and this is the central design constraint. The most likely reasons a node drops from a container-hosted cluster are the container runtime failing, the network failing, or the node agent dying — and a containerised agent is unavailable in all three. Self-healing therefore has two layers: a minimal **native supervisor** that survives what containers cannot, and a **containerised health agent** for richer diagnostics that do not need host privileges. A single containerised agent would be absent precisely when it is needed.
- Q: What may the supervisor actually change on a host? → A: Only what restores cluster membership, as set out in the Scope Boundary above: restarting the node agent, re-establishing network connectivity, re-registering and resuming heartbeat, and reclaiming allowlisted scratch paths when disk exhaustion is what is stopping the agent. Each is deterministic, idempotent, reversible, and individually auditable. Capacity concerns such as a stopped container runtime are escalated or proposed, never repaired autonomously, and crashed engines and engine-environment rollback are excluded outright because features 004, 005, and 019 already own them.
- Q: What stops a node from repairing itself on a wrong view of the world? → A: Quorum awareness. A node that cannot see a majority of the cluster must assume its own view may be the faulty one. In that state it may perform only local, non-destructive actions — restarting its own agent or re-establishing its own network — and must not take any action that risks running work, because "I have lost the cluster" and "the cluster has lost me" are indistinguishable from one side.
- Q: What stops a repair loop? → A: A circuit breaker. Each action has a maximum attempt count within a window and exponential backoff, and on exhaustion the node stops attempting, marks itself unhealthy, and escalates. A node repeatedly restarting a service is more damaging and harder to diagnose than a node that has cleanly given up and said so.

### Session 2026-08-29 (follow-up: agent framework and where it runs)

- Q: Is remediation driven by a language model? → A: The *decision* is never model-driven, but the *structure* is the same agent framework the rest of the platform uses. The harness framework allows a model to be supplied at run time rather than fixed at construction, and provides deterministic non-network execution, so one agent definition serves both paths: disconnected, it executes a deterministic symptom-to-action decision table with no model at all; connected, the same definition can call a model to explain a failure or propose an action for human confirmation. A node that has dropped from the cluster usually cannot reach a model, so the safety-critical path must never require one.
- Q: Does using that framework on a node conflict with the bare-Studio footprint? → A: No, provided the slim distribution is used. It installs the core logic without provider SDKs or other superfluous packages, which is what makes it acceptable on a node whose compute is reserved for inference. The full distribution MUST NOT be installed on a Studio for this purpose.
- Q: May the supervisor run outside the container system? → A: Yes, and it must. It runs as an operating-system service on each node, which is what lets it survive a container-runtime failure — the case a containerised agent cannot cover. This was confirmed as acceptable on 2026-08-29.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A node rejoins the cluster without a human (Priority: P1)

A node drops out for a recoverable reason, notices, diagnoses it, applies the matching fix, and rejoins — with the whole sequence recorded.

**Why this priority**: This is the feature. The common failure modes on this cluster are recoverable by restarting something, and none of them should need a terminal.

**Independent Test**: Kill the node agent on a Studio and confirm the node rejoins automatically with the diagnosis and action recorded.

**Acceptance Scenarios**:

1. **Given** a healthy node, **When** its node agent dies, **Then** the supervisor restarts it and the node rejoins the cluster without human action.
2. **Given** a node whose container runtime has stopped, **When** the supervisor detects it, **Then** it escalates a capacity diagnosis and does not restart the runtime autonomously, because the node remains a cluster member throughout.
3. **Given** a node that lost its network interface, **When** the supervisor detects it, **Then** it re-establishes the interface and re-registers with the control plane.
4. **Given** any successful remediation, **When** it completes, **Then** the symptom, the chosen action, and the outcome are recorded and reconciled to the control plane on rejoin.
5. **Given** a node that rejoins, **When** the control plane reconciles it, **Then** its running engine processes are re-adopted rather than restarted.

---

### User Story 2 - Self-healing cannot make things worse (Priority: P1)

Remediation never destroys running work, never acts outside its allowlist, and never loops.

**Why this priority**: An automatic repair capability that can damage a node holding 250 GB of loaded models is worse than no repair capability. This property is what makes the feature acceptable at all.

**Independent Test**: Induce a symptom with no allowlisted remedy and confirm the node escalates rather than improvising; induce a persistent fault and confirm the circuit breaker stops attempts.

**Acceptance Scenarios**:

1. **Given** a symptom with no matching allowlisted action, **When** it is diagnosed, **Then** the node escalates a diagnosis and takes no action.
2. **Given** a persistent fault, **When** repair attempts reach their limit within the window, **Then** the circuit breaker opens, attempts stop, the node marks itself unhealthy, and it escalates.
3. **Given** a node with a running model instance, **When** any remediation is considered, **Then** no action that would terminate running work is taken without explicit human confirmation.
4. **Given** repeated remediation, **When** attempts are made, **Then** exponential backoff is applied between them.
5. **Given** any remediation attempt, **When** it runs, **Then** it is idempotent and safe to repeat.

---

### User Story 3 - A partitioned node stays conservative (Priority: P1)

A node that has lost sight of the cluster does not assume the cluster is broken and start repairing itself aggressively.

**Why this priority**: From inside a partition, "I lost the cluster" and "the cluster lost me" are the same observation. A node acting confidently on that ambiguity is the classic way automation turns a partial outage into a total one.

**Independent Test**: Partition a Studio and confirm it restricts itself to local non-destructive actions.

**Acceptance Scenarios**:

1. **Given** a node that cannot see a majority of the cluster, **When** it evaluates remediation, **Then** it performs only local non-destructive actions.
2. **Given** that same node, **When** it considers an action affecting running work, **Then** it refuses and records why.
3. **Given** a partition that heals, **When** the node rejoins, **Then** it reconciles its recorded diagnoses and actions to the control plane.
4. **Given** a node in a minority partition, **When** it is asked to act by anything other than the control plane, **Then** it refuses.

---

### User Story 4 - The richer diagnostics run where they are safe (Priority: P2)

Deeper analysis that does not need host privileges runs in a container, keeping the privileged surface as small as possible.

**Why this priority**: It is what keeps the native, privileged component minimal and auditable. It ranks below the mechanism because the supervisor alone delivers the recovery value.

**Independent Test**: Confirm the containerised agent produces diagnostics while holding no host privileges, and that the supervisor's privileges are enumerable and narrow.

**Acceptance Scenarios**:

1. **Given** a healthy node, **When** the containerised health agent runs, **Then** it gathers diagnostics and reports them without holding host privileges.
2. **Given** the native supervisor, **When** its privileges are inspected, **Then** they are enumerated, narrowly scoped to allowlisted actions, and do not amount to general host administration.
3. **Given** the container runtime being down, **When** diagnostics are needed, **Then** the supervisor still functions and reports on its own.
4. **Given** both components, **When** their resource use is measured, **Then** both stay within the node's monitoring budget.

---

### User Story 5 - A human is told what happened and what could not be fixed (Priority: P2)

An operator sees which nodes self-healed, what was wrong, and what needs them.

**Why this priority**: Silent self-repair hides a degrading node until it fails permanently. The record is how a recurring fault becomes visible.

**Independent Test**: Trigger several remediations and one escalation, and confirm both appear to an operator.

**Acceptance Scenarios**:

1. **Given** completed remediations, **When** an operator views node health, **Then** each is listed with symptom, action, outcome, and time.
2. **Given** an escalation, **When** it occurs, **Then** it alerts and names the node and the diagnosis.
3. **Given** an open circuit breaker, **When** an operator views the node, **Then** it is shown as not self-healing with the reason and the attempt history.
4. **Given** a repeated symptom over time, **When** an operator views history, **Then** the recurrence is visible rather than each occurrence appearing isolated.
5. **Given** an escalated diagnosis, **When** the ops harness is available, **Then** it may propose a remedy for human confirmation, and MUST NOT execute one autonomously.

---

### Edge Cases

- The supervisor itself dies: the operating system's own service manager MUST restart it, and it MUST be simple enough that its own failure is unlikely.
- The supervisor and the control plane disagree about node health: the control plane's view governs cluster membership; the supervisor's view governs only local remediation.
- Disk is exhausted: only allowlisted scratch and log paths may be reclaimed; model weights, adapters, checkpoints, and job directories MUST NOT be deleted to reclaim space.
- Memory pressure looks like a fault: the supervisor MUST NOT unload models to "fix" the node, since eviction is the scheduler's decision and unloading a pinned model would break the ops path.
- A node self-heals during an upgrade: remediation MUST be inhibited while an upgrade is in progress, so it cannot fight the upgrade's own rollback.
- Both Studios attempt remediation simultaneously: actions affecting cluster capacity MUST be serialised so the cluster never loses both nodes to concurrent repair.
- A thermal condition is the real cause: throttling MUST be diagnosed as thermal and escalated, never remediated by restarting software.
- The node dropped because it was deliberately removed by an admin: remediation MUST NOT re-register a node the control plane has revoked.
- A repair succeeds but the node drops again immediately: the circuit breaker MUST count this as failure rather than success, since flapping is not recovery.
- The engine environment is broken after an upgrade: this is out of scope and MUST escalate; feature 019's automatic rollback owns it, and a second component reverting the same environment on different evidence is a fault, not a feature.
- An engine crashes while the node stays reachable: the supervisor MUST take no action, since the node never left the cluster and instance failure is owned elsewhere.
- A remedy for a membership fault would require an out-of-scope action: the supervisor MUST escalate rather than substitute an in-scope action that does not actually address the diagnosis.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Each host MUST run a minimal native supervisor that does not depend on the container runtime, the node agent, or network reachability to function.
- **FR-002**: The native supervisor MUST be restarted automatically by the operating system's service manager if it dies.
- **FR-003**: Each host MUST additionally run a containerised health agent for diagnostics that do not require host privileges.
- **FR-004**: The containerised health agent MUST hold no host privileges and MUST NOT be capable of applying host-level remediation.
- **FR-005**: The supervisor's privileges MUST be enumerated, narrowly scoped to the allowlisted actions, and MUST NOT amount to general host administration.
- **FR-006**: Remediation MUST be limited to a fixed allowlist of actions, each deterministic, idempotent, reversible, and individually auditable.
- **FR-007**: The autonomous allowlist MUST be limited to membership-restoring actions: restarting the node agent, re-establishing network connectivity, re-registering and resuming heartbeat, and reclaiming allowlisted scratch and log paths when disk exhaustion is preventing the node agent from running.
- **FR-007a**: Capacity-affecting actions, including restarting the container runtime, MUST NOT be taken autonomously; they MUST be escalated or proposed for human confirmation.
- **FR-007b**: The supervisor MUST NOT restart, stop, or otherwise act on engine processes; engine and instance failure is owned by the instance state machine and the scheduler.
- **FR-007c**: The supervisor MUST NOT roll back or alter the engine environment; that is owned by the upgrade mechanism.
- **FR-007d**: The supervisor MUST NOT touch model weights, adapters, checkpoints, job directories, user data, scheduler state, placement decisions, operating-system settings, or installed packages, and MUST NOT install, upgrade, or remove software.
- **FR-007e**: Any diagnosis whose remedy would require an out-of-scope action MUST produce an escalation, and MUST NOT be approximated with an in-scope action.
- **FR-008**: Remediation MUST be chosen by a deterministic symptom-to-action decision table, and MUST NOT be chosen by a language model.
- **FR-008a**: The supervisor MUST be able to diagnose and remediate with no model available and no network reachability, and MUST NOT depend on a model being reachable for any allowlisted action.
- **FR-008b**: Remediation actions MUST be defined as typed tools with validated arguments and outcomes, sharing the platform's agent framework and schema definitions so node-side and control-plane-side definitions cannot drift.
- **FR-008c**: The agent definition MUST leave the model unbound at construction and accept one at run time, so the same definition serves the disconnected deterministic path and the connected model-assisted path.
- **FR-008d**: Only the framework's slim distribution may be installed on a node, without provider SDKs, consistent with the inference-only footprint.
- **FR-009**: A symptom with no matching allowlisted action MUST produce an escalated diagnosis and no action.
- **FR-010**: No remediation may delete model weights, adapters, checkpoints, or job directories.
- **FR-011**: No remediation may unload or evict a model; eviction remains the scheduler's decision.
- **FR-012**: No action that would terminate running work may be taken without explicit human confirmation.
- **FR-013**: A node that cannot see a majority of the cluster MUST restrict itself to local non-destructive actions.
- **FR-014**: Each action MUST have a maximum attempt count within a window, with exponential backoff between attempts.
- **FR-015**: On attempt exhaustion the circuit breaker MUST open, attempts MUST stop, the node MUST mark itself unhealthy, and it MUST escalate.
- **FR-016**: A repair followed by an immediate recurrence MUST be counted as a failure, not a success.
- **FR-017**: Remediation MUST be inhibited while an upgrade is in progress on that node.
- **FR-018**: Actions affecting cluster capacity MUST be serialised across nodes so the cluster cannot lose both Studios to concurrent repair.
- **FR-019**: A node whose registration has been revoked MUST NOT re-register itself.
- **FR-020**: Thermal conditions MUST be diagnosed as thermal and escalated, never remediated by restarting software.
- **FR-021**: Every diagnosis, attempted action, and outcome MUST be recorded locally and reconciled to the control plane's audit trail on rejoin.
- **FR-022**: Remediation history MUST be visible per node, including recurrence over time and any open circuit breaker with its attempt history.
- **FR-023**: Escalations MUST alert, naming the node and the diagnosis.
- **FR-024**: The ops harness MAY propose a remedy for an escalated diagnosis under the existing confirmation flow, and MUST NOT execute one autonomously.
- **FR-025**: An operator MUST be able to disable self-healing per node, and that action MUST be audited.
- **FR-026**: Both components' steady-state resource use MUST stay within the node's monitoring budget, consistent with the Studios' inference-only footprint.

### Key Entities

- **Node Supervisor**: The minimal native component. Host, version, privileges held, state, last evaluation, circuit-breaker state.
- **Health Agent**: The containerised diagnostic component. Host, version, diagnostics collected, reporting state.
- **Symptom**: An observed fault condition. Name, detection signals, severity, quorum requirement for action.
- **Remediation Action**: An allowlisted repair. Name, symptom mapping, preconditions, reversibility, attempt limit, backoff policy, privileges required.
- **Remediation Attempt**: One execution. Node, symptom, action, outcome, duration, attempt number, circuit-breaker state after, reconciled-at.
- **Escalation**: A diagnosis requiring a human. Node, symptom, evidence, proposed action if any, alert reference, resolved-at.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A node whose agent is killed rejoins the cluster automatically, with no human action, in 100% of trials.
- **SC-002**: A node whose container runtime is stopped recovers its run capacity automatically.
- **SC-003**: The supervisor continues to function and report with the container runtime stopped.
- **SC-004**: A symptom outside the allowlist produces an escalation and zero remediation actions, in 100% of trials.
- **SC-005**: No remediation ever deletes model weights, adapters, or checkpoints, or unloads a model, across the full test suite.
- **SC-006**: A persistent fault opens the circuit breaker within its configured limit and stops further attempts.
- **SC-007**: A node in a minority partition takes only local non-destructive actions, in 100% of induced partitions.
- **SC-008**: Concurrent remediation never removes both Studios from service simultaneously.
- **SC-009**: Every remediation attempt is reconciled into the control plane's audit trail once the node rejoins.
- **SC-010**: The containerised agent holds no host privileges, verified by inspection.
- **SC-011**: Both components stay within the node monitoring budget under full inference load.
- **SC-012**: Remediation is inhibited during an upgrade, verified by attempting both concurrently.
- **SC-013**: Every autonomous action taken maps to restoring node-agent liveness, network reachability, or registration; no action outside that mandate is ever taken, across the full fault-injection suite.
- **SC-014**: A crashed engine on a reachable node produces zero supervisor actions.
- **SC-015**: A stopped container runtime produces an escalation and zero autonomous restarts.

## Assumptions

- Features 009, 019, and 020 have shipped: the health model with heartbeats, damping and freshness; the upgrade and rollback mechanism this feature reuses for environment rollback; and the quorum view this feature consults before acting.
- The native supervisor runs as an operating-system service outside the container runtime, which the user confirmed on 2026-08-29 is acceptable and is what makes it survive container-runtime failure.
- The supervisor is built on the platform's existing agent framework, using its slim distribution so no provider SDKs are installed on a node. The framework is used for its typed-tool and validated-output machinery, not for model access: the model is left unbound at construction and supplied only when one is reachable. Its deterministic non-network execution mode is what the disconnected path uses.
- Most process-supervision responsibility — keeping a service alive across crashes and reboots — is delegated to the operating system's own service manager rather than reimplemented.
- The containerised health agent runs on the container runtime already installed on all three hosts (verified 2026-08-29), so no new runtime is introduced.
- The Studios' inference-only footprint applies: both components are small, budgeted, enumerable, and removable.
- Remediation is deterministic by design. Model-driven diagnosis is available only as an explanation or a proposal through the ops harness under human confirmation, per feature 012.
- The allowlist is configuration, reviewable and extendable without changing the remediation mechanism. Extending it is a deliberate act with its own review, not an implementation detail.
- Constitutional position: the native supervisor is a function of the node agent's existing remit, which Principle II already permits on a Studio. The containerised health agent is an additional container on a Studio and should be confirmed against Principle II during the plan's Constitution Check — it is far narrower than feature 020's web tier, but it is not nothing.
- Per Principle VII this feature requires documented manual verification on the real cluster before merge; fault injection against a real node cannot be meaningfully exercised in CI.
