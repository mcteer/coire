# Feature Specification: Upgrades and Rollback

**Feature Branch**: `feat/019-upgrades-rollback-plan`

**Roadmap ID**: 015 (feature directory 019)

**Created**: 2026-08-29 · **Revised**: 2026-10-10

**Status**: Specified for implementation planning

**Input**: Versioned Studio engine environments, smoke testing, atomic activation and automatic rollback; service-by-service control-plane upgrades; administrator controls and progress.

## Overview

Administrators need to upgrade the Studios and control plane without losing work, private data, or the ability to recover when the new version fails. A candidate is prepared beside the running release, checked against an explicit compatibility record, and activated only after affected work drains. Failure restores the exact known-good release automatically. An administrator can also request a compatible rollback after a successful upgrade.

This revision uses merged 018 as its baseline. Existing training, evaluation, feedback, immutable installation, and deployment safeguards remain binding. The older, separate 019 worktree is reference material, not completed acceptance evidence.

## Clarifications

### Session 2026-10-10

The ambiguity review retained the original draft's parallel installation, real inference smoke, automatic recovery, and manual OS-update boundaries. It corrects “previous tag” to exact release identity, makes migration compatibility a prerequisite, and accounts for all work introduced through 018.

- Q: May control-plane upgrades use a brief, announced maintenance window after active requests drain? → A: Allow a bounded maintenance window, with clear retry guidance and no forced termination of training or agent work.

A1 records this accepted policy and its proposed timing defaults. Preservation, authentication, workload ownership, and real-cluster validation follow existing project requirements.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — A bad Studio upgrade restores service (Priority: P1)

An administrator selects a prepared engine release. A broken candidate must leave the source installation intact or restore it automatically, including when the node agent stops responding.

**Why this priority**: Automatic recovery is the roadmap's named acceptance bar.

**Independent Test**: Exercise failures before activation, after activation, and during recovery; verify the exact source release serves a fresh request after recovery.

**Acceptance Scenarios**:

1. **Given** an installed source release, **when** candidate verification or real inference fails before activation, **then** the source remains selected and the failure is recorded.
2. **Given** an activated candidate, **when** its health, identity, or required qualification fails or times out, **then** an independent recovery owner restores the source and proves it healthy.
3. **Given** interrupted recovery, **when** the agent, recovery owner, or host restarts, **then** the accepted operation resumes without a second activation or unsafe admission.
4. **Given** unavailable source files or uncertain process ownership, **when** recovery cannot be proved, **then** the target stays unavailable for placement and an actionable recovery alert identifies the obstacle.

### User Story 2 — A good Studio upgrade and later rollback are controlled (Priority: P1)

An administrator upgrades one Studio, observes its exact installed release, and may later restore the retained compatible release. Existing training and other work finish safely before activation.

**Why this priority**: Successful upgrades must preserve work and measured capability guarantees.

**Independent Test**: Upgrade one Studio, prove single-node and required paired operation, then request rollback and prove the restored runtime; race new work against maintenance admission.

**Acceptance Scenarios**:

1. **Given** active requests or jobs, **when** maintenance starts, **then** affected new work is rejected with a retry reason and existing work finishes; timeout aborts the upgrade without killing the work.
2. **Given** a fully drained target and valid candidate, **when** activation completes, **then** installed and observed identities agree and service resumes only after required checks pass.
3. **Given** paired serving is supported, **when** either Studio's runtime changes, **then** paired placement remains unavailable until a real two-participant test succeeds for the exact pair of releases.
4. **Given** a retained compatible source, **when** rollback is requested, **then** the same admission, verification, recovery, and audit rules apply. Incompatible or missing sources produce a precise refusal.
5. **Given** concurrent upgrade requests, **when** either Studio or the control plane already has an operation in progress, **then** the second mutation is refused without starting another activation.

### User Story 3 — The control plane upgrades and recovers service by service (Priority: P1)

An administrator chooses a prepared release and sees compatibility and expected maintenance before starting. A failed service causes automatic restoration of every service already changed by that operation.

**Why this priority**: Recovery must remain available while the API and scheduler themselves are replaced.

**Independent Test**: Roll a compatible release, inject migration and service failures, restart the recovery owner, and verify the complete original service cohort is restored without changing protected data or secrets.

**Acceptance Scenarios**:

1. **Given** incompatible retained data, missing backup evidence, unprepared images, or an unavailable recovery owner, **when** an upgrade is requested, **then** activation is refused with the unmet prerequisite.
2. **Given** successful preflight and drain, **when** a required migration fails, **then** no application service is replaced; uncertain schema state keeps maintenance active for recovery.
3. **Given** successful migration, **when** services roll, **then** each reaches the required health and identity before the next changes.
4. **Given** a service fails after earlier services changed, **when** recovery runs, **then** all changed services return to the recorded source cohort, retaining compatible additive data changes.
5. **Given** gateway replacement, **when** the maintenance window begins, **then** existing requests have drained; new requests receive a retry response while the edge is available, and reconnect after temporary gateway unavailability.
6. **Given** an unrelated service restarts outside gateway maintenance, **when** chat is streaming, **then** that restart does not break the stream. Existing failover fencing remains authoritative throughout recovery.

### User Story 4 — Administrators operate and observe upgrades (Priority: P2)

An administrator uses the console or CLI to view versions, choose prepared releases, start upgrades, cancel safely, request rollback, and inspect durable progress and failure evidence.

**Why this priority**: Operators need the same truthful state through each supported interface.

**Independent Test**: Complete console and CLI workflows, disconnect progress during API replacement, reconnect to the same operation, and verify audit history and permission enforcement.

**Acceptance Scenarios**:

1. **Given** prepared releases and installed targets, **when** the version view opens, **then** desired, observed, retained, incompatible, and drifted states are distinguishable.
2. **Given** a submitted operation, **when** a request or event connection is retried, **then** it resumes the original operation without a duplicate mutation or missing terminal result.
3. **Given** cancellation before activation, **when** the administrator cancels, **then** safe cleanup releases only that operation's resources. After activation, cancellation requests recovery rather than abandoning a partially changed target.
4. **Given** a non-admin, revoked actor, malformed target, or replayed execution command, **when** a mutation is attempted, **then** it is rejected and protected state does not change.
5. **Given** a failure, **when** an administrator opens its details, **then** the stage, redacted evidence, recovery status, and next operational action are visible and accessible by keyboard.

### Edge Cases

Insufficient staging space; source or candidate tampering; same package version with different contents; missing tiny-model asset; full or unreadable journals; process identifier reuse; unknown or orphaned workers; lease expiry during drain; two administrators racing; dropped activation response; rollback failure; host reboot at every durable stage; partial two-Studio upgrade; incompatible schema or native journal; active emergency failover; deliberately disabled diagnostics; immutable source checkout removed; changed credentials; interrupted feedback export or erasure; unavailable retained release.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Studio candidates MUST be installed beside the source, from pinned and integrity-verified release artifacts, without modifying either the active or retained installation.
- **FR-002**: Only administrator-prepared releases with exact content identity and declared compatibility MUST be selectable. Requests MUST NOT supply arbitrary commands, filesystem paths, model strings, or mutable version tags as execution authority.
- **FR-003**: A Studio candidate MUST pass a real, already-acquired tiny-model smoke generating at least ten tokens before activation and prove serving after activation. Required paired capability MUST pass a real two-participant collective before the upgrade succeeds.
- **FR-004**: Studio activation and restoration MUST select one complete installation atomically; observed identity MUST match the selected release before admission reopens.
- **FR-005**: Candidate failure before activation MUST leave the source selected, preserve its files, and record bounded, redacted failure evidence.
- **FR-006**: Failure after activation or an expired health deadline MUST automatically restore the exact source. Recovery MUST survive loss of the agent, API, scheduler, recovery process, and host restart; uncertain recovery MUST retain maintenance and alert.
- **FR-007**: An administrator MUST be able to request rollback to the retained compatible release using the same checks as an upgrade; unavailable or incompatible targets MUST be refused explicitly.
- **FR-008**: Staging MUST reserve sufficient disk before writes and protect current, rollback, and active-operation artifacts from cleanup; insufficient capacity MUST cause refusal without eviction of user assets.
- **FR-009**: Maintenance MUST atomically stop new affected admissions before draining, cover both scheduling and execution, and retain ownership until terminal reconciliation. Unknown inventory or missing stop evidence MUST block activation.
- **FR-010**: Drain MUST include serving, training and measurements, evaluation, images, agent runs, acquisition/replication, imports/exports/extractions, private-data cleanup, probes, and outstanding resource or renewal obligations. It MUST NOT forcibly terminate these workloads; deadline expiry aborts the upgrade before activation.
- **FR-011**: At most one cluster upgrade or rollback operation MUST own activation at a time. Paired probes may reserve both Studios but MUST NOT activate both together.
- **FR-012**: A runtime change MUST invalidate affected link and measured workload qualifications, including changes with unchanged package version. Historical results, datasets, and adapters MUST remain intact; new workload admission requires qualification valid for the exact installed runtime.
- **FR-013**: Control-plane upgrades MUST verify rollback-compatible schema and data formats and usable backup evidence before migration; required migrations MUST complete successfully before any application service replacement.
- **FR-014**: The declared control-plane service cohort MUST roll sequentially, with health and exact image identity checked after each replacement. Persistent infrastructure outside that cohort MUST not be replaced as a side effect.
- **FR-015**: A failed rollout MUST stop advancing and restore the entire changed cohort in reverse order to its recorded source identities, then verify the source service set.
- **FR-016**: Control-plane execution and recovery MUST remain operational while the API and scheduler are unavailable and reconcile durable progress when they return.
- **FR-017**: Automated rollback MUST stay within a tested compatibility set and MUST NOT downgrade production data, restore a backup over newer data, erase retained history, or silently transform older training/feedback formats. Unsupported rollback MUST state the required manual procedure.
- **FR-018**: The console and CLI MUST show prepared, desired, observed and retained releases, readiness, drift, and incompatibility reasons, and offer upgrade, rollback, status and cancellation actions with the same permissions.
- **FR-019**: Operation stages and redacted evidence MUST be durable and replayable after connection loss, including the terminal result and restoration status; retrying a mutation MUST not create a second operation.
- **FR-020**: Every administrative mutation and stage outcome MUST retain actor, target, source and destination identities, timestamps and outcome in audit history. Metrics, traces, bounded logs, a dashboard, and failure/stalled-recovery alerts MUST be provided; disabling diagnostics MUST not disable audit or alerts.
- **FR-021**: Reported or installed identity differing from the authorized identity MUST be visible as drift and MUST block successful activation or reopening admission.
- **FR-022**: OS, firmware, container-runtime and database-major upgrades MUST remain outside this automation. Studio OS changes MUST invalidate affected qualification and require the existing real interconnect checks before paired placement resumes.
- **FR-023**: Gateway replacement MAY use the bounded maintenance policy in A1. Existing requests MUST drain first, unrelated service restarts MUST preserve streams, and recovery MUST respect existing failover election and hand-back requirements.
- **FR-024**: Only authenticated, currently authorized human administrators MUST initiate upgrades and rollbacks. Internal execution authority MUST be restricted to the accepted operation and target; revocation before activation MUST prevent activation, while necessary restoration remains permitted.
- **FR-025**: Accepted commands, claims and receipts MUST be fenced against stale owners, replay, conflicting source state, and duplicate execution; recovery MUST reconcile actual installed state before repeating effects.
- **FR-026**: Cancellation before activation MUST safely unwind staging; cancellation after activation MUST complete bounded restoration. It MUST never abandon uncertain workers, release their holds, or report success while recovery remains incomplete.
- **FR-027**: Deployment MUST preserve project isolation, immutable credential generations, security settings, mounted data, and source-independent release records. Upgrades MUST not regenerate credentials or widen network, socket, firewall or capability permissions.
- **FR-028**: Existing feedback opt-out and deletion semantics MUST survive upgrade and rollback: published datasets and trained adapters are preserved; unexported contributions stay excluded and subject to their existing purge obligations. Historical training/evaluation data and private erasure obligations MUST remain usable and auditable.
- **FR-029**: Fresh installation MUST establish an independently recoverable upgrade mechanism before accepting managed upgrades; missing recovery support MUST be reported as not ready, never bypassed.
- **FR-030**: Automated unit, contract, disposable-environment recovery and tiny-model integration tests, plus documented real-cluster drain, paired qualification and rollback acceptance, MUST pass before implementation is declared complete.

### Key Entities

- **Release**: Prepared, immutable content identity; target kind; artifact provenance; supported source, data and protocol compatibility; required smoke capabilities.
- **Installed Target**: Desired, observed, retained release identities; readiness and qualification; source of drift evidence.
- **Upgrade Operation**: One actor-authorized upgrade or rollback, target, idempotency identity, deadlines, stages, source/target releases and terminal outcome.
- **Maintenance Reservation**: Durable ownership and generation that prevents new affected work and records drain blockers.
- **Execution Receipt**: Ordered evidence of an intended or completed side effect and its actual installed result, recoverable after disconnection.
- **Smoke Result**: Exact release and model identity, check, duration, outcome and bounded redacted evidence.
- **Compatibility Record**: Tested relationships between source and candidate binaries, retained schema and workload formats, including rollback limits.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All enumerated pre-activation and post-activation fault cases preserve or restore the exact source; with the host available, automatic restoration proves a fresh serving request within five minutes of failure detection. Otherwise the target stays fenced and a recovery alert fires within one minute of the missed recovery deadline.
- **SC-002**: A compatible Studio upgrade and later requested rollback each complete real ten-token smoke and report the exact expected identity; same-version content changes do not retain stale qualification.
- **SC-003**: Every injected service health failure restores all already-changed services to their source identities, including failures while replacing the API or scheduler and after executor restart.
- **SC-004**: Each failed or incompatible migration test replaces zero application services and preserves data; uncertain schema state prevents reopening admission.
- **SC-005**: In a 100-request streaming test, unrelated service restarts drop zero active streams; maintenance begins only after active affected requests finish, and new affected requests receive retry guidance while the ingress is reachable.
- **SC-006**: Concurrent and replayed operation tests produce at most one activation owner, zero duplicate activations, and zero workloads admitted through a maintenance fence.
- **SC-007**: Paired placement remains blocked until a real collective passes for the exact two runtime identities; failed paired qualification triggers restoration of the changed Studio and does not alter the peer's release.
- **SC-008**: Every accepted mutation and terminal outcome has a complete audit record. Reconnected console and CLI clients recover the same operation and terminal result within ten seconds of API readiness, without resubmitting it.
- **SC-009**: Insufficient disk, missing artifacts, unready recovery support and unknown work ownership cause refusal before activation, preserve user assets, and expose a specific blocking reason.
- **SC-010**: For each workload class in FR-010, race and drain-timeout tests cause zero forced workload terminations and zero premature release of its resource holds.
- **SC-011**: Upgrade/rollback preservation tests retain credential bytes and mounted generation identity, published dataset and adapter hashes, feedback opt-out state, training/evaluation history, private erasure obligations and project boundaries.
- **SC-012**: The implementation evidence includes passing API/node/executor/CLI/browser contracts, every durable-boundary recovery case, tiny-model integration, image hardening/build/scan checks and measured real-cluster acceptance, with no required check skipped.

## Assumptions and Scope

- **A1 — Confirmed availability policy**: A brief announced gateway maintenance window is acceptable. Drain defaults to 15 minutes and aborts the upgrade on expiry. Candidate readiness defaults to two minutes per target/service; restoration defaults to five minutes after failure detection. Gateway unavailability has a five-minute success budget; exceeding it is a failed upgrade requiring recovery, not a claim of uninterrupted service. The maintenance policy is user-confirmed; numeric deadlines are conservative planning defaults.
- The implementation baseline is merged 018 at `3a872ef8f82b2a0c73969bc433b4616d079227c4`. Existing versioned Studio installations, source-independent Compose releases, and compatible tiny assets are prerequisites to validate, not assumed absent.
- Administrators stage artifacts through the existing trusted build/deployment path. This feature does not provide a general package manager, an arbitrary deployment scripting service, or automatic model acquisition.
- Managed control-plane rollouts cover the first-party application cohort declared in the plan. Initial recovery-component installation and its own replacement use the separately health-gated deployment bootstrap described there; infrastructure version upgrades remain separate operations.
- Compatibility is declared and tested per release. “One version back” is not sufficient proof, especially across pre-018 privacy and native training format boundaries.
- Persistent models may be unloaded after their active requests finish and restored through normal ownership paths. No spare serving capacity or uninterrupted gateway availability is assumed.
- Tests use isolated fixtures in CI. Coding-agent acceptance on the real Studios is authorized by AGENTS.md and required during implementation; this planning run makes no runtime changes.
