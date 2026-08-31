# Feature Specification: Upgrades and Rollback

**Feature Branch**: `019-upgrades-rollback`

**Roadmap ID**: 015 (Phase 4 — Chat UI, images, training)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "Versioned engine envs on nodes, smoke test, symlink flip, rollback; control-plane image/`uv sync` upgrade job; admin UI trigger."

## Overview

Everything the platform depends on will need upgrading: the inference and training engines on the Studios, and the control-plane images on core. This feature makes both routine and reversible. A node upgrade builds a new versioned environment beside the current one, proves it with a smoke test, switches atomically, and rolls back automatically on failure. A control-plane upgrade rolls images service by service with migrations run separately. The acceptance bar is deliberately adversarial: a deliberately broken engine pin must roll back on its own and leave the node serving.

## Clarifications

### Session 2026-08-29

- Q: Why versioned environments beside each other rather than upgrading in place? → A: Because in-place upgrade has no cheap reverse. Building a new environment alongside the current one makes the switch a pointer change and the rollback the same pointer change back, which is the only way rollback is fast enough to happen automatically on a failed smoke test.
- Q: What does the node smoke test prove? → A: That the new environment can actually serve — loading a tiny model and generating tokens — and, when the node participates in sharded serving, that a two-rank collective operation succeeds. Anything less would pass an environment that imports cleanly and fails on first real use.
- Q: When does rollback happen automatically versus on request? → A: Automatically whenever the smoke test fails or the agent fails to come back healthy within its window. Rollback after a successful upgrade — because something was noticed later — is an explicit admin action.
- Q: How do control-plane upgrades avoid dropping traffic? → A: Migrations run first as a one-shot that must exit successfully; images then roll service by service, each waiting for health before the next. Rollback is re-pinning the previous tag. This is exactly the per-service independence feature 000 established, used for its intended purpose.
- Q: Are operating-system upgrades in scope? → A: No. They remain manual, because the Studio-to-Studio interconnect's RDMA capability is tied to the OS build and must be re-validated after either Studio updates. This feature triggers re-validation of that two-Studio link when a Studio returns; core has no RDMA role per feature 022.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A bad engine upgrade rolls itself back (Priority: P1)

An admin upgrades a node's engine environment to a broken version. The smoke test fails, the node reverts automatically, and it continues serving.

**Why this priority**: This is the roadmap's named acceptance bar and the property that makes upgrading safe enough to do routinely.

**Independent Test**: Upgrade a node to a deliberately broken pin and confirm automatic rollback with serving intact.

**Acceptance Scenarios**:

1. **Given** a node serving normally, **When** an upgrade to a broken version is triggered, **Then** the new environment is built without disturbing the running one.
2. **Given** that new environment, **When** the smoke test runs and fails, **Then** the switch is not made and the failure is recorded with the test output.
3. **Given** an automatic rollback, **When** it completes, **Then** the node is serving on its previous environment.
4. **Given** the whole sequence, **When** it ends, **Then** the node is healthy and no manual intervention was required.

---

### User Story 2 - A good engine upgrade takes effect cleanly (Priority: P1)

An admin upgrades a node to a working version; the smoke test passes, the switch happens, the agent restarts, and the node returns to service on the new environment.

**Why this priority**: The success path must be equally reliable, or upgrades will be avoided and the platform will drift onto unmaintained pins.

**Independent Test**: Upgrade a node to a working version and confirm it serves on the new environment afterwards.

**Acceptance Scenarios**:

1. **Given** a working target version, **When** the upgrade runs, **Then** the smoke test passes and the switch is made atomically.
2. **Given** the switch, **When** the node agent restarts, **Then** it returns healthy and reports the new version.
3. **Given** a node that participates in sharded serving, **When** it is upgraded, **Then** the link probe re-runs before it is eligible for tensor-parallel placements again.
4. **Given** a successful upgrade, **When** an admin later requests rollback, **Then** the previous environment is restored by the same mechanism.

---

### User Story 3 - The control plane upgrades service by service (Priority: P1)

An admin upgrades the control plane; migrations run first, images roll one service at a time, and a failure re-pins the previous tag.

**Why this priority**: This is how every fix and feature reaches production. It ranks alongside node upgrades rather than below them.

**Independent Test**: Upgrade the control plane to a new tag and confirm each service rolls independently with health gating.

**Acceptance Scenarios**:

1. **Given** a new tag, **When** an upgrade is triggered, **Then** migrations run as a one-shot that must exit successfully before any service is replaced.
2. **Given** successful migrations, **When** services roll, **Then** each is replaced and confirmed healthy before the next.
3. **Given** a service that fails to become healthy, **When** the failure is detected, **Then** the upgrade stops and the previous tag is restored for that service.
4. **Given** a failed migration, **When** it exits non-zero, **Then** no service is replaced.
5. **Given** a rolling upgrade, **When** it proceeds, **Then** streaming requests in flight are not dropped by unrelated services restarting.

---

### User Story 4 - Upgrades are triggered and observed from the console (Priority: P2)

An admin sees current versions, triggers an upgrade, and watches it progress without a terminal.

**Why this priority**: Required by the constitutional "reachable without a terminal" bar, but the mechanism must work before the interface matters.

**Independent Test**: Trigger a node upgrade from the console and follow it to completion.

**Acceptance Scenarios**:

1. **Given** the console, **When** an admin views versions, **Then** each node's engine environment version and each control-plane service's image tag are shown.
2. **Given** an upgrade, **When** it is triggered from the console, **Then** its stages and outcome stream as it runs.
3. **Given** a failed upgrade, **When** it fails, **Then** the failing stage and its output are shown.
4. **Given** any upgrade or rollback, **When** it completes, **Then** it is audited with actor, target, versions, and outcome.

---

### Edge Cases

- Disk is insufficient to build a new environment beside the current one: the upgrade MUST be refused before starting rather than failing partway.
- A node holds a running instance when an upgrade is triggered: the upgrade MUST drain or refuse rather than killing a serving process without warning.
- The node agent does not return healthy after the switch: rollback MUST trigger automatically on a timeout, not wait for a human.
- Both nodes are upgraded simultaneously: the system MUST prevent it, or the platform loses all serving capacity at once.
- A control-plane upgrade changes the database schema incompatibly with the running version: migrations and image rolls MUST be ordered so no service runs against a schema it cannot use.
- Rollback is requested after a migration that cannot be reversed: the limitation MUST be stated explicitly rather than presenting rollback as unconditionally safe.
- An upgrade is triggered while a training job is running: it MUST be refused or deferred rather than destroying hours of work.
- The engine version pinned in the lockfile disagrees with what a node reports: the discrepancy MUST be visible.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Node engine environments MUST be versioned and installed beside the current one, never in place.
- **FR-002**: Environment contents MUST be pinned by a lockfile held in the repository.
- **FR-003**: An upgrade MUST run a smoke test that loads a small model and generates tokens, and additionally verifies a two-rank collective operation on nodes that participate in sharded serving.
- **FR-004**: The switch to a new environment MUST be atomic.
- **FR-005**: A failed smoke test MUST prevent the switch and MUST record the test output.
- **FR-006**: Failure of the node agent to return healthy within a configured window MUST trigger automatic rollback.
- **FR-007**: Rollback after a successful upgrade MUST be available as an explicit admin action using the same mechanism.
- **FR-008**: A node upgrade MUST refuse to start when disk is insufficient to build the new environment.
- **FR-009**: A node upgrade MUST drain running instances or refuse, and MUST NOT terminate a serving process without warning.
- **FR-010**: An upgrade MUST be refused or deferred while a training job is running on that node.
- **FR-011**: The system MUST prevent both Studios from being upgraded simultaneously.
- **FR-012**: The link probe MUST re-run when an upgraded node returns, before it is eligible for tensor-parallel placement.
- **FR-013**: Control-plane upgrades MUST run migrations as a one-shot that must exit successfully before any service is replaced.
- **FR-014**: Control-plane services MUST roll one at a time, each confirmed healthy before the next.
- **FR-015**: A service failing to become healthy MUST stop the upgrade and restore its previous tag.
- **FR-016**: Rollback of a control-plane service MUST be achieved by re-pinning its previous image tag.
- **FR-017**: Irreversible migrations MUST be identified, and rollback MUST NOT be presented as unconditionally safe when one has run.
- **FR-018**: The console MUST show current node environment versions and service image tags, and MUST allow triggering upgrades and rollbacks.
- **FR-019**: Upgrade progress, stages, and failure output MUST be observable as the upgrade runs.
- **FR-020**: Every upgrade and rollback MUST be audited with actor, target, source and destination versions, and outcome.
- **FR-021**: Discrepancies between pinned versions and node-reported versions MUST be visible.
- **FR-022**: Operating-system upgrades MUST remain manual and out of this feature's automation.

### Key Entities

- **Node Environment**: A versioned engine installation. Node, version, lockfile reference, installed-at, active flag, smoke-test result.
- **Upgrade Job**: One upgrade attempt. Target (node or control plane), source and destination versions, stages with outcomes, smoke-test output, rollback performed, actor, timings.
- **Service Version**: A control-plane service's deployed image. Service, image tag, digest, deployed-at, healthy flag, previous tag.
- **Smoke Test Result**: Evidence an environment works. Environment, checks performed, outcome per check, captured output, run-at.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An intentionally broken engine pin rolls back automatically and leaves the node serving, in 100% of trials.
- **SC-002**: A working upgrade completes, the node returns healthy, and it reports the new version.
- **SC-003**: A control-plane upgrade rolls service by service with health gating, and a failing service restores its previous tag without manual intervention.
- **SC-004**: A failed migration results in no service being replaced.
- **SC-005**: Unrelated services restarting during a rolling upgrade do not drop in-flight streaming requests.
- **SC-006**: Both Studios can never be upgraded simultaneously.
- **SC-007**: An upgraded node is ineligible for tensor-parallel placement until its link probe re-runs.
- **SC-008**: Every upgrade and rollback produces an audit row naming actor, target, versions, and outcome.
- **SC-009**: An upgrade attempted with insufficient disk is refused before any change is made.

## Assumptions

- Features 000–018 have shipped. In particular, feature 000 established one-service-one-container with health gating and one-shot migrations, which this feature uses for its intended purpose rather than introducing new mechanism.
- Node environments live under a versioned path on each Studio with an active-version pointer; the Studios currently have no such installation (verified 2026-08-29), so feature 000's node preparation creates the first one.
- Control-plane images are built from a git tag by CI, multi-architecture with the architecture that actually runs built first, per feature 000.
- The link probe and tensor-parallel gating come from feature 006; if that feature has not shipped, the re-probe requirement applies to reachability only.
- Backups of the system of record are an operational concern outside this feature; the model store is reproducible and excluded from backup.
- Only the pinned admin model's service and the container runtime itself are updated outside this mechanism.
- Operating-system upgrades remain manual because interconnect capability is tied to the OS build and must be re-validated after each one.
- Per Principle VII, this feature requires a documented manual verification on the real cluster before merge, since automatic rollback cannot be meaningfully exercised in CI.
