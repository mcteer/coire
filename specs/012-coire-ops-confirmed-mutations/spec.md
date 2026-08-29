# Feature Specification: Coire-Ops with Confirmed Mutations

**Feature Branch**: `012-coire-ops-confirmed-mutations`

**Roadmap ID**: 010 (Phase 3 — Agents)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "Long-lived `coire-ops` container on core (the only harness core runs) using the pinned admin model on Studio B via the gateway; ops tools for admin actions with `confirm_token` flow; admin UI approval prompt; read-only degraded mode when the admin model is unreachable."

## Overview

Feature 008 gave the console a read-only "ask Coire" box. This feature makes the operator agent able to act — but only through an explicit human approval step. It is the one harness core is permitted to run, it calls a model that lives elsewhere, and every mutation it proposes must be confirmed by an admin against a token bound to that exact proposed action before anything happens. The design goal is that the agent's usefulness never becomes the platform's largest unaudited privilege.

## Clarifications

### Session 2026-08-29

- Q: What does the confirmation token bind to? → A: The exact resolved action — the operation, its target, and its parameters — plus the proposing conversation, with a short expiry and single use. Approving "unload the idle 400B model" must not be redeemable as an unload of something else, so the token cannot be a generic permission to proceed.
- Q: Who may approve? → A: An authenticated admin acting in the console, never the agent and never a service token. The approval is a distinct human action against the presented proposal, and the resulting mutation is audited as the admin's action with the agent recorded as proposer.
- Q: What happens when the admin model on Studio B is unreachable? → A: The ops harness degrades to a read-only status responder that uses no model at all — reporting state from the control plane directly. It never falls back to running a model on core, because core has no engine and Principle II forbids it.
- Q: Which mutations may it propose? → A: A fixed allowlist of operational actions — unload, kill a run, pin and unpin, load, and similar reversible operations. Irreversible actions such as retiring a model or deleting a user are not proposable at all; those stay in the console's own flows.
- Q: Does the ops agent have filesystem, shell, or git access? → A: None. Its only toolset is the platform's admin API. It ships in its own image without the tools the coding profile carries.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An admin asks for an action and approves it (Priority: P1)

An admin types a request in plain language, the agent proposes a specific action, the console presents it for approval, and on approval the action executes and is audited.

**Why this priority**: This is the feature, and the roadmap's named acceptance bar.

**Independent Test**: Ask for an idle model to be unloaded, approve the resulting proposal, and confirm the unload happened and was audited.

**Acceptance Scenarios**:

1. **Given** an idle loaded model, **When** an admin asks for it to be unloaded, **Then** the agent proposes a specific unload naming the model and instance, and no action occurs yet.
2. **Given** that proposal, **When** the console presents it, **Then** the admin sees the exact operation, target, and parameters before deciding.
3. **Given** an approved proposal, **When** the admin confirms, **Then** the action executes and an audit row records the admin as actor and the agent as proposer.
4. **Given** a declined proposal, **When** the admin declines, **Then** nothing executes and the decline is recorded.
5. **Given** an expired proposal, **When** approval is attempted, **Then** it is refused and a fresh proposal is required.

---

### User Story 2 - A confirmation cannot be redirected (Priority: P1)

An approval for one action cannot cause a different action, however the request is replayed or altered.

**Why this priority**: This is the security property that makes the whole flow trustworthy. A token that approves "an unload" rather than "this unload" is a confused-deputy vulnerability with admin privileges.

**Independent Test**: Capture a confirmation token, alter the action or target, and confirm the mutation is refused.

**Acceptance Scenarios**:

1. **Given** a token issued for one action, **When** it is presented with different parameters, **Then** the mutation is refused and the attempt is recorded.
2. **Given** a token already used, **When** it is presented again, **Then** it is refused.
3. **Given** a token past its expiry, **When** it is presented, **Then** it is refused.
4. **Given** any mutating ops route, **When** it is called without a valid token, **Then** it is refused.
5. **Given** a non-admin, **When** they attempt to approve a proposal, **Then** it is refused.

---

### User Story 3 - The agent stays useful when its model is gone (Priority: P2)

Studio B is down and the admin can still ask what is happening, receiving a factual status answer rather than an error.

**Why this priority**: The moment the operator agent is most wanted is when something is broken, which is exactly when its model may be unavailable.

**Independent Test**: Make the admin model unreachable and confirm the box still answers status questions and refuses to propose actions.

**Acceptance Scenarios**:

1. **Given** the admin model unreachable, **When** an admin asks about state, **Then** a factual status answer drawn from the control plane is returned, with its degraded nature stated.
2. **Given** the same condition, **When** an admin asks for an action, **Then** the agent explains it cannot propose actions while degraded rather than failing obscurely.
3. **Given** the admin model returning, **When** it becomes reachable, **Then** full capability resumes without an operator restart.
4. **Given** any degraded state, **When** it is active, **Then** no model is loaded on core at any point.

---

### User Story 4 - Irreversible actions stay out of reach (Priority: P2)

The agent cannot propose destructive or irreversible operations at all.

**Why this priority**: Limiting the blast radius by construction is stronger than limiting it by prompt, and this is cheap to enforce at the toolset boundary.

**Independent Test**: Ask the agent to retire a model or delete a user and confirm it has no such capability.

**Acceptance Scenarios**:

1. **Given** a request to retire a model or delete a user, **When** it is made, **Then** the agent reports it cannot do so and no proposal is produced.
2. **Given** the ops toolset, **When** it is enumerated, **Then** it contains only allowlisted reversible operational actions.
3. **Given** the ops container, **When** it is inspected, **Then** it contains no filesystem, shell, or git tooling.

---

### Edge Cases

- The proposed action becomes stale between proposal and approval — the model was already unloaded: execution MUST detect the changed state and report it rather than acting blindly or erroring opaquely.
- The agent proposes an action against a target that no longer exists: approval MUST fail with a clear reason.
- Two admins approve the same proposal concurrently: exactly one MUST execute, since the token is single-use.
- The agent is asked to do something it has no tool for: it MUST say so plainly rather than approximating with a different tool.
- The agent's model produces a malformed proposal: it MUST be rejected by validation and never surfaced as an approvable action.
- The ops container restarts mid-conversation: pending proposals MUST NOT survive as approvable, since their context is gone.
- The agent is asked to approve its own proposal: no path may exist for the agent to supply a confirmation.
- Core is under memory pressure: the ops container MUST be constrained so it cannot destabilise the control plane.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The ops harness MUST run as a single long-lived container on core and MUST be the only harness core runs.
- **FR-002**: The ops harness MUST call the pinned admin model through the gateway, and MUST NOT load or run any model on core.
- **FR-003**: The ops toolset MUST consist only of platform admin API operations, and MUST contain no filesystem, shell, or git tooling.
- **FR-004**: Proposable mutations MUST be limited to an allowlist of reversible operational actions; irreversible operations MUST NOT be proposable.
- **FR-005**: A proposed mutation MUST NOT execute until an authenticated admin confirms it.
- **FR-006**: A confirmation token MUST bind to the exact operation, target, and parameters, and to the proposing conversation.
- **FR-007**: A confirmation token MUST be single-use and MUST expire after a short configured interval.
- **FR-008**: A token presented with different parameters, after use, or after expiry MUST be refused and the attempt recorded.
- **FR-009**: Every mutating ops route MUST require a valid confirmation token.
- **FR-010**: Only an authenticated admin may confirm; neither the agent nor a service token may.
- **FR-011**: An executed mutation MUST be audited with the admin as actor and the agent as proposer.
- **FR-012**: A declined proposal MUST be recorded.
- **FR-013**: Execution MUST detect state that changed between proposal and approval and report it rather than acting on stale assumptions.
- **FR-014**: When the admin model is unreachable, the harness MUST degrade to a read-only status responder using no model, and MUST state that it is degraded.
- **FR-015**: While degraded, the harness MUST refuse to propose actions.
- **FR-016**: Full capability MUST resume automatically when the admin model becomes reachable.
- **FR-017**: A malformed proposal from the model MUST be rejected by validation and never surfaced as approvable.
- **FR-018**: Pending proposals MUST NOT survive an ops container restart as approvable.
- **FR-019**: The ops container MUST be resource-constrained so it cannot destabilise the control plane on core.
- **FR-020**: The console MUST present each proposal with its full resolved operation, target, and parameters before the admin decides.

### Key Entities

- **Proposal**: A pending agent-proposed mutation. Identity, conversation, operation, target, resolved parameters, natural-language rationale, created-at, expiry, state.
- **Confirmation Token**: The single-use authority to execute one proposal. Proposal binding, parameter digest, expiry, used-at, issued-to.
- **Ops Conversation**: An admin's session with the operator agent. Admin, messages, proposals produced, degraded flag.
- **Ops Tool**: An allowlisted operational capability. Name, operation, reversibility, required role, parameter schema.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Asking to unload an idle model produces a confirmation card and, on approval, an audited unload.
- **SC-002**: A confirmation token altered in any parameter is refused in 100% of attempts.
- **SC-003**: A confirmation token is never redeemable twice.
- **SC-004**: No mutation executes without an admin confirmation, verified across the full ops toolset.
- **SC-005**: Every executed mutation is audited naming both the confirming admin and the proposing agent.
- **SC-006**: With the admin model unreachable, status questions are still answered and action requests are refused with a stated degraded reason.
- **SC-007**: No model is ever loaded on core, verified by inspection during both normal and degraded operation.
- **SC-008**: Irreversible operations are absent from the ops toolset entirely.

## Assumptions

- Features 001–011 have shipped: the gateway, the pinned admin model on Studio B, credentials and audit, the console, and the agent harness with its ops profile.
- Feature 008's read-only ask box is extended by this feature rather than replaced; the console gains a proposal and approval surface.
- The ops image is separate from the user-facing agent image and contains the admin client, per feature 010.
- The pinned admin model is a small model resident on Studio B with an idle TTL of never, so it is available whenever Studio B is.
- Confirmation expiry is a configurable interval with a short documented default.
- The allowlist of proposable operations is configuration, reviewable and extendable without changing the confirmation mechanism.
- Runs started by users are feature 011 and are unrelated to this long-lived container, which is not a user run.
