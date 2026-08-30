# Feature Specification: Admin Console

**Feature Branch**: `008-admin-console`

**Roadmap ID**: 006 (Phase 2 — Identity, users, admin)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "React admin routes: nodes & memory ledger, models (add from HF / publish / unpublish / retire / pin / load / unload / convert, with download & replication progress, disk per Studio, per-task defaults), users & keys, runs & jobs with kill, upgrades, audit viewer, 'ask Coire' box wired to the ops agent (read-only until 010)."

## Overview

Everything the platform can do is currently reachable only from a terminal. This feature puts the whole operational surface behind a role-gated set of routes in the SPA: the cluster and its memory ledger, the model roster and its acquisition pipeline, users and keys, running jobs with a kill switch, and the audit trail. Its acceptance bar is a constitutional one — every operation Principles I and II describe must be reachable without a terminal.

## Clarifications

### Session 2026-08-29

- Q: One SPA or two? → A: One SPA with role-gated routes, as the architecture specifies. Admin routes are hidden and refused for non-admins, and the refusal is enforced server-side, never only by hiding navigation. A second application would double the build, ingress, and auth surface for no benefit.
- Q: How live must the console be? → A: Anything with a running state — instance transitions, download and replication progress, job progress, ledger occupancy — streams. Static inventory such as user lists is fetched on navigation. Polling the ledger on a timer would be both laggier and heavier than the event streams features 002 and 005 already produce.
- Q: What can the "ask Coire" box do in this feature? → A: Read-only questions answered by the ops harness against the pinned admin model. It may describe state and explain what it would do, but exposes no mutating tool. Confirmed mutations arrive in feature 012, which adds the confirmation flow rather than changing this surface.
- Q: What happens when the console shows a control whose backing feature has not shipped? → A: It is not shown. The console is built incrementally alongside features; a control appears when the capability behind it exists. Disabled placeholder controls train operators to distrust the interface.
- Q: Does the console show per-Studio disk? → A: Yes, prominently, alongside memory. The two-copies-of-everything rule means the roster is bounded by the smaller Studio's free disk, so disk is a first-class capacity signal, not a detail.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An admin sees the cluster at a glance (Priority: P1)

An admin opens the console and immediately sees both Studios and core: health state, live CPU and GPU utilisation, thermal state, memory budget and what occupies it, free disk, and every running instance.

**Why this priority**: This view answers the first question of every operational conversation, and every other admin screen is navigated from it.

**Independent Test**: With models loaded across both nodes, open the console and confirm the displayed state matches actual cluster state.

**Acceptance Scenarios**:

1. **Given** a running cluster, **When** an admin opens the cluster view, **Then** each node's health state, live CPU and GPU utilisation, thermal state, memory budget, reservations with their holders, free memory, and free disk are shown.
2. **Given** a node that is degraded rather than dead, **When** the cluster view is shown, **Then** it is visibly distinguished from both healthy and unreachable nodes, with the reason and time since last heartbeat.
3. **Given** an instance changing state, **When** the transition occurs, **Then** the view updates without a manual refresh.
4. **Given** a node that becomes unreachable, **When** it does, **Then** it is shown as unreachable and its instances are not shown as ready.
5. **Given** ledger drift beyond threshold, **When** it occurs, **Then** it is surfaced visibly rather than only in metrics.
6. **Given** node health data outside its freshness window, **When** the view is shown, **Then** it is marked stale rather than presented as current.

---

### User Story 2 - An admin manages the model roster end to end (Priority: P1)

An admin adds a model from a repo id, watches it through the acquisition pipeline, sets its curation, publishes it, and can pin, load, unload, or retire it — all without a terminal.

**Why this priority**: The roster is the platform's product. This is the largest single block of the constitutional "reachable without a terminal" bar.

**Independent Test**: Add a small model from the console and take it all the way to published and loaded.

**Acceptance Scenarios**:

1. **Given** an admin, **When** they add a model by repo id with a target precision, **Then** the acquisition pipeline starts and each stage's progress is visible as it runs.
2. **Given** a model in replication, **When** the admin views it, **Then** per-Studio copy status and bytes transferred are shown.
3. **Given** a `ready` model, **When** the admin sets tags, description, capability profile, placement, idle TTL, visibility, and entitlements, **Then** the changes take effect and are audited.
4. **Given** a published model, **When** the admin pins, loads, unloads, or retires it, **Then** the action executes and the resulting state is reflected.
5. **Given** several variants, **When** the admin views the model, **Then** validation results per variant are shown and one can be made the picker default.
6. **Given** a failed acquisition, **When** the admin views it, **Then** the failing stage and its reason are shown, with a retry that resumes rather than restarts.

---

### User Story 3 - An admin can stop something that is running (Priority: P1)

An admin finds a running job, agent run, or instance and stops it, and the stop takes effect promptly.

**Why this priority**: The kill switch is a safety property. A public platform whose operator cannot stop a runaway process from the interface is not operable.

**Independent Test**: Start a long-running job, kill it from the console, and confirm it stops.

**Acceptance Scenarios**:

1. **Given** running jobs and runs, **When** the admin views them, **Then** each is listed with its kind, owner, target, elapsed time, and state.
2. **Given** a running job, **When** the admin kills it, **Then** it stops promptly and its state and reason are updated.
3. **Given** a running agent run, **When** the admin kills it, **Then** the run stops and its credential is invalidated.
4. **Given** a killed item, **When** the admin views the audit trail, **Then** the kill is recorded with actor and target.

---

### User Story 4 - An admin manages users, keys, and entitlements (Priority: P2)

An admin creates users, assigns roles, issues and revokes scoped keys, and grants or revokes entitlements from the interface.

**Why this priority**: Necessary for the platform to have users other than its operator, but the platform functions for its operator without it.

**Independent Test**: Create a user, issue a scoped key, grant an entitlement, then revoke both.

**Acceptance Scenarios**:

1. **Given** an admin, **When** they create a user and assign a role, **Then** the user can authenticate and is limited to that role.
2. **Given** a user, **When** the admin issues a key with scopes, a rate limit, and a budget, **Then** the secret is shown exactly once and thereafter only metadata is visible.
3. **Given** an active key, **When** the admin revokes it, **Then** it stops working immediately and consumption to date remains visible.
4. **Given** a user, **When** the admin grants or revokes an entitlement, **Then** it takes effect and is audited distinctly.

---

### User Story 5 - An admin asks the platform about itself (Priority: P3)

An admin types a question into an "ask Coire" box and gets an answer grounded in live platform state, without any action being taken.

**Why this priority**: Genuinely useful, but strictly additive; every fact it reports is available on another screen. It also depends on the pinned admin model being resident.

**Independent Test**: Ask a question about current cluster state and confirm the answer matches the cluster view and that nothing was mutated.

**Acceptance Scenarios**:

1. **Given** a running cluster, **When** an admin asks about current state, **Then** an answer grounded in live state is returned.
2. **Given** any question, **When** it is answered, **Then** no mutating action is taken and no mutating capability is exposed.
3. **Given** the admin model unreachable, **When** a question is asked, **Then** the box degrades to a clear unavailable state rather than failing obscurely.

---

### Edge Cases

- A non-admin navigates directly to an admin route: the server MUST refuse regardless of what the client renders.
- An admin's role is revoked mid-session: the next admin action MUST be refused without requiring a reload to become safe.
- A long-running acquisition is open in two browser tabs: both MUST show consistent progress without duplicating the job.
- A stream disconnects: the view MUST reconnect and reconcile rather than silently freezing on stale state.
- The console renders a very large audit trail or model list: it MUST paginate rather than attempting to render everything.
- A destructive action such as retire or revoke is chosen: it MUST require explicit confirmation naming the target.
- A control's backing feature has not shipped: the control MUST be absent, not disabled.
- Two admins edit one model concurrently: the second write MUST be detected and refused or merged, never silently overwritten.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Admin functionality MUST be role-gated routes within the single SPA, and access MUST be enforced server-side independently of client rendering.
- **FR-002**: The console MUST show, per node, health state, live CPU and GPU utilisation, thermal state, memory budget, every reservation with its holder, free memory, and free disk.
- **FR-002a**: The console MUST visually distinguish `healthy`, `degraded`, and `unreachable` nodes, showing the reason and time since last heartbeat for anything not healthy.
- **FR-002b**: The console MUST indicate when a node's health data is outside its freshness window, rather than presenting stale values as current.
- **FR-003**: The console MUST surface ledger drift beyond threshold visibly.
- **FR-004**: The console MUST allow adding a model by repo id with a target precision and MUST show every acquisition stage's progress live.
- **FR-005**: The console MUST show per-Studio copy status and transfer progress during replication.
- **FR-006**: The console MUST allow setting tags, description, capability profile, placement policy, idle TTL, visibility, entitlements, and per-task defaults.
- **FR-007**: The console MUST allow publish, unpublish, retire, pin, unpin, load, unload, and convert.
- **FR-008**: The console MUST show per-variant validation results and allow designating a picker default.
- **FR-009**: A failed acquisition MUST show its failing stage and reason, with a retry that resumes from the last completed stage.
- **FR-010**: The console MUST list running jobs, agent runs, and instances with kind, owner, target, elapsed time, and state.
- **FR-011**: The console MUST allow killing a job, run, or instance, and a killed run's credential MUST be invalidated.
- **FR-012**: The console MUST allow creating users, assigning roles, issuing and revoking scoped keys, and granting and revoking entitlements.
- **FR-013**: A newly-issued key's secret MUST be shown exactly once and never again.
- **FR-014**: The console MUST provide a paginated, filterable audit viewer.
- **FR-015**: The console MUST provide a read-only "ask Coire" box answered by the ops harness, exposing no mutating capability.
- **FR-016**: The ask box MUST degrade to a clear unavailable state when the admin model is unreachable.
- **FR-017**: Live-state views MUST update by streaming rather than polling, and MUST reconnect and reconcile after a disconnect.
- **FR-018**: Destructive actions MUST require explicit confirmation naming the target.
- **FR-019**: Concurrent edits to one entity MUST be detected and refused or merged, never silently overwritten.
- **FR-020**: Controls for capabilities that do not exist MUST be absent rather than disabled.
- **FR-021**: Large collections MUST be paginated.

### Key Entities

- **Console Session**: An authenticated admin's view state. User, role, granted routes, active subscriptions.
- **Cluster View**: The aggregate operational picture. Nodes with capacity, health state, live CPU and GPU utilisation and thermal state, instances with state, ledger occupancy and drift, health freshness.
- **Roster View**: The model roster as curated. Models with state, variants, validation results, curation fields, per-node copy status.
- **Activity View**: Running work. Jobs, agent runs, and instances with owner, elapsed time, state, and kill affordance.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every operation described by Principles I and II is reachable from the console without a terminal, verified against an enumerated checklist of those operations.
- **SC-002**: A model can be taken from repo id to published and loaded entirely from the console.
- **SC-003**: A non-admin is refused on 100% of admin routes server-side, independent of client rendering.
- **SC-004**: Live views reflect a state change within 2 seconds of it occurring, without manual refresh.
- **SC-004a**: Per-node CPU and GPU utilisation are visible on the cluster view and refresh within their configured interval.
- **SC-004b**: A degraded node is visually distinguishable from a healthy and an unreachable one, in 100% of trials.
- **SC-005**: A running job or agent run is stopped from the console within 5 seconds of the kill action.
- **SC-006**: A newly-issued key secret is retrievable exactly once, verified by attempting to retrieve it again.
- **SC-007**: A dropped stream reconnects and reconciles without leaving stale state on screen.
- **SC-008**: Every destructive console action produces an audit row naming actor and target.

## Assumptions

- Features 000–007 have shipped: the acquisition pipeline, ledger, instances, and the auth, key, entitlement, and audit model all exist and expose the state this console reads.
- The console is served by the same SPA and ingress as the chat UI, which arrives in feature 014; this feature builds the shared application shell and the admin routes within it.
- Live updates reuse the event streams features 002 and 005 already produce rather than introducing a new transport.
- The ops harness exists in a read-only form sufficient to answer questions; the confirmed-mutation flow is feature 012.
- Upgrade controls are surfaced here only to the extent feature 019 has shipped; if it has not, those controls are absent per FR-020.
- Image, training, and evaluation views arrive with features 015, 016, and 017 and extend this console rather than replacing it.
- Dashboards for metrics and traces live in the observability stack from feature 009 and are linked to, not reimplemented here.

## Design reference

The visual design for the console is specified, not left to implementation. `docs/design/DESIGN.md`
is the source of truth for look; this spec and `docs/ARCHITECTURE.md` remain the source of truth for
behaviour. Where the two disagree on appearance, the design specification wins; where they disagree
on what a control does, this spec wins.

Because this feature builds the shared application shell, it owns the shell contract in §2 of that
document — ground, brand and breadcrumb, status chips, the five-item dock, and the sub-tab bar — for
every later page to inherit. It must also establish `docs/design/tokens.css` as the only source of
colour, radius, shadow, and spacing values in `apps/coire-web`, since §1 forbids hard-coding any
value that already has a token.

- `docs/design/mockups/admin.html` — Overview tile grid, node tiles with segmented ledger bars, the
  roster table, and Ask Coire's confirm-button pattern (the audit-writing flow itself is feature 012).
- `docs/design/mockups/settings.html` — the sub-tab pattern this feature must generalise.

The mockups are static 1440×900 frames carrying literal values rather than tokens, and their sample
data is placeholder. Their measurements are authoritative; their content is not.
