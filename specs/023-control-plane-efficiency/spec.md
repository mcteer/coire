# Feature Specification: Quiet, reliable control plane

**Feature Branch**: `feat/023-control-plane-efficiency`
**Created**: 2026-09-26
**Status**: Implementation and validation
**Input**: Fix all worthwhile findings from the control-plane architecture review, across deployment, background work, health, and the interface, in a single PR. Issue: #24.

## User Scenarios & Testing

### User Story 1 — A trustworthy, quiet deployment (Priority: P1)

As the operator, I can start a coherent control plane and see whether its dependencies actually work, without configuration drift, repeated error floods, or accidental local builds.

**Independent Test**: Bring up an isolated local deployment; simulate missing configuration and unavailable database credentials; verify preflight/readiness errors and bounded retries without altering persisted user data.

**Acceptance Scenarios**:
1. Given an installed release, when the development checkout changes, then release configuration remains unchanged and restart still works.
2. Given a required configuration file or secret is unavailable, when startup runs, then it fails before partially replacing the running configuration.
3. Given database access fails, when dependency health/startup readiness is checked, then it reports unavailable without exposing credentials; retries back off and recover automatically. Existing process liveness remains a separate signal.
4. Given ordinary startup, when the operator starts the release, then installed image IDs are
   reused and no pull or compilation runs unless explicitly requested.

### User Story 2 — Background work has a bounded cost (Priority: P1)

As the operator, I can keep the control plane running alongside desktop applications with only the capabilities I need active, while queued work, recovery, and safety controls remain responsive.

**Independent Test**: Exercise worker lifecycle, idle/failing/recovering polling, service profiles, and a durable job against local fixtures. Verify that API restart does not own or duplicate command execution.

**Acceptance Scenarios**:
1. Given no queued work, when idle continues, then worker scans back off to a documented ceiling and do not create unbounded log output.
2. Given a queued command or kill request, when it is processed, then existing correctness, recovery, and kill deadlines still hold, including when another command is waiting for a long-running run to finish.
3. Given the lean deployment, when it starts, then unused optional capabilities are absent and telemetry never targets disabled backends.
4. Given full diagnostics are disabled, when the operator inspects configuration, then historical trace/log-search limitations are explicit while audit records, health metrics, alerts, and bounded local logs remain available.

### User Story 3 — Monitoring does useful work only (Priority: P2)

As an administrator, I see current, truthfully labelled capacity and state without each open page repeating the same expensive work or a hidden page continuously refreshing.

**Independent Test**: Connect multiple console clients; vary only timestamps, then real state; hide/show the page and interrupt the stream. Verify bounded snapshot assembly, appropriate events/reconnection, and capacity labels.

**Acceptance Scenarios**:
1. Given unchanged domain state, when observation timestamps advance, then they alone do not trigger full snapshot events.
2. Given multiple authorized viewers, when they read within the refresh window, then common snapshot assembly is reused and returns current data after expiry.
3. Given a hidden page, when background time passes, then its dashboard stream closes; returning to the page reconnects and reconciles.
4. Given connection failures, when retries occur, then delays increase to a bounded ceiling and cancellation releases readers/timers.
5. Given runtime-only capacity measurements, when shown, then they are explicitly labelled as runtime measurements and unavailable CPU utilization is not fabricated.

### Edge Cases

- Worker/database failure during shutdown; startup fails after only some components start.
- Multiple admins, cache expiry races, reconnect cursors, last viewer disconnects, browser online/offline and visibility changes.
- Missing release files, invalid or partial secrets, credential drift against an existing database, and re-running installation for the same release.
- Optional service profiles and trace exporters must agree; existing audit, revocation, failover and engine-placement guarantees remain intact.

## Requirements

### Functional Requirements

- **FR-001**: Deployment MUST provide validated, stable release configuration outside the changing checkout, explicit image-build opt-in, and a documented rollback path.
- **FR-002**: Secret preparation MUST validate all required inputs before changing active files; recovery MUST preserve persisted data and keep credentials out of outputs, command arguments, and the repository.
- **FR-003**: Dependency health and startup readiness MUST reflect required database/runtime dependencies with bounded response time; existing `/ready` process-liveness semantics MUST remain compatible, and liveness probing MUST avoid high-frequency interpreter startup.
- **FR-004**: Background command execution MUST have one owning service and bounded retry/idle scanning; shutdown MUST clean up partially started components.
- **FR-005**: Existing kill/revocation, lease, and failover deadlines MUST remain unchanged. No real Studio engine or container is started by this work.
- **FR-006**: Operators MUST be able to enable optional capabilities explicitly and choose lean or historical diagnostics coverage; defaults MUST have bounded resources/log retention and valid exporters.
- **FR-007**: Common console snapshot work MUST be reused for a configurable short interval, perform each ledger projection once, and suppress events caused only by observation timestamps.
- **FR-008**: Hidden dashboard pages MUST stop streaming; reconnection MUST use bounded backoff and clean cancellation. Expensive visual effects MUST respect reduced-motion/transparency preferences.
- **FR-009**: Capacity output MUST identify its measurement scope and return unknown for unsupported utilization instead of substituting load average.
- **FR-010**: Changes MUST retain authenticated contracts, audit history, bare engines, Studio-only user workloads, durable recovery, and security boundaries.
- **FR-011**: Regression coverage, operational runbooks, efficiency metrics/dashboard/alerts, generated contract artifacts, and measured local remediation evidence MUST accompany the single PR.
- **FR-012**: A long-running command MUST NOT block the kill path. A run MUST NOT be reported or audited as killed until termination is confirmed, or it is established that no container was ever created; failure MUST remain retryable without restoring revoked tokens.
- **FR-013**: After a validated lean replacement is healthy, the rollout MUST remove stopped or orphaned Coire project containers belonging to disabled capabilities, without deleting volumes, rollback release inputs, or unrelated containers.

### Key Entities

- Console snapshot: authorized current domain state and separately identified observation metadata.
- Runtime capacity: observed memory/disk quantities, measurement scope, and optional measured utilization.
- Deployment release: immutable configuration, selected capabilities, pinned image references, external secrets and persistent volumes.
- Background worker: owner, lifecycle, last outcome, and bounded next-attempt delay.

## Success Criteria

- **SC-001**: Two simultaneous console viewers cause at most one common projection per refresh window; timestamp-only changes produce no replacement event.
- **SC-002**: Hidden-page tests observe no open dashboard stream and visibility restoration produces a fresh reconciliation.
- **SC-003**: Required-dependency failure produces unavailable dependency health within three seconds; persistent failures back off rather than logging full stack traces every second.
- **SC-004**: Lean deployment contains only enabled capabilities and continuous baseline monitoring; disabled telemetry destinations receive no retries.
- **SC-005**: Switching the source checkout cannot change an installed release's mounted configuration; missing inputs fail preflight before service mutation.
- **SC-006**: Existing lifecycle/kill/failover tests plus new regression tests pass; live remediation records whether authentication/export churn was removed, without claiming unmeasured desktop-frame improvement.
- **SC-007**: With a responsive node, a kill request stops its run within the existing five-second bound even while a wait command is blocked; unsuccessful termination never produces a false successful terminal state or completion audit.
- **SC-008**: After rollout, no disabled or orphaned Coire containers remain; the PostgreSQL volume and unrelated containers are still present.

## Assumptions

- One PR is explicitly requested, overriding the usual size/splitting guideline. It targets the existing feature integration branch containing feature 020.
- Retain PostgreSQL, durable scheduling, containers and native Studio agents. Native core migration and database replacement have no measured justification here.
- Lean diagnostics trades continuous historical traces/centralized log search for lower steady footprint; this requires a narrow constitution/architecture amendment, recorded with the implementation.
- Resource ceilings are conservative initial limits, not promised consumption; no global OrbStack limit change is automatic before representative validation.
- Regression and integration tests are required by the repository workflow; engine behavior is unchanged, so these fixes use local services and stubs rather than loading a model.
- The existing runtime-capacity contract already contains a source discriminator. Reuse it in the interface; no new host exporter or invented host measurements are required.
