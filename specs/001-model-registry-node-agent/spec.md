# Feature Specification: Model Registry and Node Agent

**Feature Branch**: `001-model-registry-node-agent`

**Roadmap ID**: 001 (Phase 1 — Single-node inference works end to end)

**Created**: 2026-08-29

**Status**: Planned

**Input**: User description: "Admin-only model registry with placement policy, memory estimate, idle TTL, visibility/entitlement, and capability profile; download job that pulls once from HF, verifies, and peer-replicates so the model is `ready` only when both Studios hold it; node agent that can load (`mlx_lm.server`), health-check, report memory and disk, and unload."

## Overview

This feature makes a model a first-class, admin-curated registry object and gives the node agent the ability to actually run one. It covers the registry record and its lifecycle states, the admin-only path for adding a model, the pull-once-then-replicate download job that makes a model `ready` only when both Studios hold a verified copy, and the node agent's load / health-check / report / unload verbs against a bare `mlx_lm.server`. It deliberately stops short of routing user traffic (feature 003), scheduling and eviction policy (feature 004), and conversion or quantisation (feature 002).

## Clarifications

### Session 2026-08-29

- Q: Which of the acquisition stages belong here versus feature 002? → A: This feature implements pull, verify, and replicate for repos that are already MLX-format and need no conversion. Inspect, convert, and validate — and the rejection of GGUF-only repos — are feature 002. A model added here whose repo is not MLX-format is rejected with a message pointing at feature 002's pipeline.
- Q: What makes a replicated copy "verified"? → A: A per-file checksum manifest recorded at pull time, re-computed on the peer after replication and compared file-by-file. A mismatch on any file leaves the model `failed` with the offending paths recorded; partial copies are deleted rather than retried in place.
- Q: How does the registry stay truthful about processes after a node agent restart? → A: The node agent re-adopts running engine processes rather than killing them. On start it enumerates engine processes it owns, matches them to registry instances by a recorded process identity, adopts the matches, and reports any unmatched process as an orphan for the control plane to reconcile. Adoption is what the acceptance bar means by "reflects true process state".
- Q: Does memory estimation need to be accurate, or is a declared number enough? → A: Both. The registry stores an estimate derived from weight bytes times a per-precision overhead factor, and the node agent reports measured resident memory once loaded. The delta is recorded on the instance so feature 004's ledger has real data and Principle-driven drift alerting has a baseline. This feature does not act on the drift.
- Q: What distinguishes an admin from a non-admin before feature 007 exists? → A: An interim static admin bearer token (ADR-0004) presented through the auth seam feature 000 declared. Every other caller — no credential, or any credential that is not that token — is the anonymous principal and is refused on every acquisition and curation route. "Non-admin caller" throughout this spec means exactly that.
- Q: What does "resume" mean for an interrupted pull? → A: Resume at file granularity. The Hugging Face client keeps completed files and re-fetches only files that were incomplete when the interruption happened; a partially transferred file starts over. Hub repositories shard weights at ≤ 5 GB, so an interruption costs at most one shard; a single-file repo restarts that file. Never marks the model `ready` on a partial file set.
- Q: What is the readiness probe for an engine? → A: A generation request issued by the node agent — a one-token completion — succeeding against the engine's port. The engine's own liveness endpoint answers before the model is loaded and is used only as a "process is listening" gate, never as readiness.
- Q: Can an admin add a model that fits on neither Studio? → A: Yes, but it is accepted as `failed` with a reason rather than silently queued: the estimate is computed at add time and a model that cannot fit any supported placement is rejected before any bytes are pulled.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Admin adds a model and it becomes servable (Priority: P1)

An admin names a Hugging Face repo, and Coire pulls it once to whichever Studio has the most free disk, verifies it, replicates it to the peer, and marks it `ready` only when both copies verify. The admin watches progress the whole way.

**Why this priority**: Nothing else in Phase 1 has anything to serve until a model can get onto the cluster under Coire's control.

**Independent Test**: Add one small MLX-format repo as an admin and confirm it reaches `ready` with a verified copy on both Studios, visible in the registry.

**Acceptance Scenarios**:

1. **Given** an admin credential and an MLX-format repo id, **When** the admin adds the model, **Then** a registry row is created in `downloading`, progress is observable, and the model reaches `ready` with verified copies on both Studios.
2. **Given** a model mid-download, **When** the admin inspects it, **Then** bytes transferred, percentage, target node, and current stage are visible.
3. **Given** a model whose peer replication fails checksum verification, **When** the job completes, **Then** the model is `failed` with the mismatching files recorded, and the partial copy is removed.
4. **Given** a model already present, **When** an admin adds the same repo again, **Then** the request is rejected as a duplicate rather than re-pulling.

---

### User Story 2 - Only admins may acquire models (Priority: P1)

A non-admin caller — no credential, or any credential that is not the admin token (ADR-0004) — attempting to add, download, or delete a model is refused, and no automation or agent has a path to trigger an acquisition.

**Why this priority**: Principle V makes this a constitutional boundary, and it is far cheaper to enforce from the first registry route than to retrofit.

**Independent Test**: Attempt every acquisition route with no credential and with a wrong bearer and confirm each returns 403 with no side effect and one audit record.

**Acceptance Scenarios**:

1. **Given** a non-admin caller, **When** it calls the model-add route, **Then** the response is 403, an audit record with outcome `refused` is written, and no registry row, job, or download is created.
2. **Given** any non-admin caller, **When** it attempts to delete or retire a model, **Then** the response is 403 and the model is untouched.
3. **Given** the Hugging Face credential, **When** any container other than the node agent is inspected, **Then** the credential is absent from its environment and filesystem.

---

### User Story 3 - Node agent loads, reports, and unloads an engine (Priority: P1)

The control plane asks a Studio to load a model; the node agent starts an engine process, reports when it is genuinely ready to serve, reports its resident memory, and unloads it cleanly on request.

**Why this priority**: This is the primitive every later serving feature composes. Without a trustworthy load/unload verb there is no routing, no scheduling, and no sharding.

**Independent Test**: Issue load, poll health until ready, read reported memory, issue unload, and confirm the process is gone and memory released.

**Acceptance Scenarios**:

1. **Given** a `ready` model, **When** the control plane issues a load to a node, **Then** an engine process starts and the node reports the model ready to serve only once the node agent's generation probe succeeds against it.
2. **Given** a loaded model, **When** the control plane requests node status, **Then** it receives live node CPU and GPU utilisation, thermal state, node memory used and free, per-process CPU utilisation and resident memory, and free disk.
3. **Given** a loaded model, **When** the control plane issues an unload, **Then** the process terminates, memory is released, and a subsequent status shows the model not loaded.
4. **Given** a load that fails because the engine exits during startup, **When** the failure occurs, **Then** the node reports the failure with the engine's exit status and captured output rather than reporting ready.

---

### User Story 4 - Registry survives a node agent restart without lying (Priority: P2)

A node agent is restarted while a model is loaded. It re-adopts the running engine rather than killing it, and the registry continues to reflect what is genuinely running.

**Why this priority**: A restart that orphans engines silently corrupts every downstream memory accounting decision; this is the acceptance bar the roadmap names explicitly.

**Independent Test**: Load a model, restart the node agent, and confirm the engine still serves and the registry still shows it loaded.

**Acceptance Scenarios**:

1. **Given** a loaded model, **When** the node agent restarts, **Then** the engine process keeps running and is re-adopted, and the model remains reported as loaded.
2. **Given** an engine process the agent cannot match to any registry instance after restart, **When** reconciliation runs, **Then** it is reported as an orphan rather than silently adopted or silently killed.
3. **Given** a model recorded as loaded whose engine died while the agent was down, **When** the agent restarts, **Then** the registry is corrected to not-loaded and the discrepancy is recorded.

---

### User Story 5 - Admin curates what exists and who may see it (Priority: P3)

An admin sets visibility, entitlement, tags, capability profile, placement policy, and idle TTL on a model, and can unpublish or retire it.

**Why this priority**: The curation surface is what features 003 and 008 read; it is needed before a picker exists but is not required to prove the acquisition and load path works.

**Independent Test**: Set a model to published, confirm it appears to an entitled caller in a registry listing, unpublish it, and confirm it disappears without unloading.

**Acceptance Scenarios**:

1. **Given** a `ready` model, **When** an admin publishes it with tags and a description, **Then** entitled callers see it in a registry listing and non-entitled callers do not.
2. **Given** a published model, **When** an admin unpublishes it, **Then** it disappears from user-visible listings immediately, is not unloaded, and its files remain.
3. **Given** a published model, **When** an admin retires it, **Then** it is unloaded, both copies are deleted, and the registry row is retained for audit.

---

### Edge Cases

- Both Studios lack the disk to hold a copy: the model MUST be rejected at add time with the required and available figures, before any bytes move.
- A pull is interrupted by a network failure: it MUST resume at file granularity — completed files are never re-fetched — and MUST NOT mark the model `ready` on a partial file set.
- A pull is interrupted by a node reboot: the job MUST resume after the node returns rather than stall indefinitely or orphan a partial directory.
- The peer Studio is unreachable when replication starts: the model MUST remain in a replicating state with the reason recorded, and MUST NOT be marked `ready` on one copy.
- The repo requires licence acceptance or is gated: the failure MUST name gating specifically rather than presenting as a generic download error.
- An unload is requested for a model with in-flight requests: this feature unloads on request; draining semantics belong to feature 005's instance state machine.
- A load is requested for a model already loaded on that node: it MUST be a no-op returning the existing process rather than starting a second engine.
- Two loads of different models are requested concurrently and together exceed the node budget: this feature MUST refuse the second with a budget error; eviction policy is feature 004.
- An engine process is killed externally: the node agent MUST detect it within its health interval and report the model as not loaded.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST store each model as a registry record carrying repo id, store key (the slug every node's copy path derives from), state, visibility, entitlement allowlist, tags, description, placement policy, memory estimate, idle TTL, chat template override (null means the repository's own), capability profile, and per-node copy status.
- **FR-002**: Model state MUST be one of `downloading`, `replicating`, `ready`, `failed`, or `retired`, and transitions MUST be recorded with a timestamp and reason.
- **FR-003**: Adding, downloading, publishing, unpublishing, retiring, and deleting a model MUST require the admin role; every such mutation MUST write an audit record.
- **FR-004**: No user request, agent, or automation may trigger a model acquisition; the system MUST expose no such path.
- **FR-005**: The Hugging Face credential MUST exist only on the node agent, and MUST NOT be present in any user-facing container.
- **FR-006**: A download job MUST pull once to the Studio with the most free disk, with resume support and a per-file checksum manifest.
- **FR-007**: The system MUST replicate the pulled model to the peer Studio over the Thunderbolt mesh rather than performing a second external pull, and MUST NOT use the internet-egress interface for peer replication. Feature 022 preserves this behavior while narrowing the mesh to the direct Studio-only data fabric.
- **FR-008**: A model MUST become `ready` only when both Studios hold a copy whose per-file checksums match the manifest.
- **FR-009**: A checksum mismatch MUST leave the model `failed` with the offending paths recorded, and the partial copy MUST be removed.
- **FR-010**: The system MUST compute a memory estimate at add time and MUST reject a model that fits no supported placement before transferring any bytes.
- **FR-011**: The node agent MUST expose load, unload, status, and health verbs to the control plane, and MUST authenticate every caller.
- **FR-012**: The node agent MUST report a model as ready to serve only after a generation request it issues succeeds against the engine, never merely on process start or on the engine's liveness endpoint.
- **FR-013**: The node agent MUST report live node CPU and GPU utilisation, thermal state, node memory used and free, per-process CPU utilisation and resident memory, and free disk.
- **FR-014**: The node agent MUST record measured resident memory against the registry estimate for each load.
- **FR-015**: On restart the node agent MUST re-adopt engine processes it owns rather than terminating them, MUST correct registry rows whose engines died, and MUST report unmatched processes as orphans.
- **FR-016**: The node agent MUST detect an externally-killed engine within its health interval and report the model as not loaded.
- **FR-017**: The system MUST never pass a caller-supplied string as a model or adapter identifier to an engine; only registry-resolved local paths may be used.
- **FR-018**: Engine processes MUST bind only the node's Thunderbolt mesh address, so they are reachable only from hosts on the unrouted mesh — the node agent, the control plane, and the peer Studio — and MUST NOT be exposed on the egress interface or any other. Per-host firewall restriction of the engine port to core is feature 005. **Superseded by feature 022:** engines move to the control endpoint and remain restricted to core and the local node agent; this historical requirement records the feature-001 delivery state.
- **FR-019**: A load for a model already loaded on that node MUST be a no-op returning the existing process.
- **FR-020**: A load that would exceed the node's memory budget MUST be refused with a budget error in this feature.

### Key Entities

- **Model**: A registry record. Repo id, store key (slug), state, visibility, entitlement, tags, description, placement policy, memory estimate, idle TTL, chat template override, capability profile, per-node copy status, timestamps.
- **Capability Profile**: Declared model behaviour. Tool-calling mode, structured-output mode, context window, reasoning style, parallel-tool support, verification status.
- **Model Copy**: A model's presence on one node. Node, path, byte size, checksum manifest reference, verified flag, verified-at timestamp.
- **Download Job**: An acquisition unit of work. Model, target node, stage, bytes transferred and total, resume state, failure reason.
- **Engine Process**: A running engine owned by a node agent. Model, node, port, process identity, started-at, measured resident memory, health state.
- **Node**: A Studio. Identity, DNS name, memory total and budget, disk total and free, live CPU and GPU utilisation, thermal state, reachability, degraded flag, last-heartbeat time.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An admin adds a small MLX-format model and it reaches `ready` with verified copies on both Studios, unattended.
- **SC-002**: Every non-admin caller (no credential, or a credential that is not the admin token) receives 403 on 100% of acquisition and curation routes, with no side effect beyond an audit record.
- **SC-003**: A model is never `ready` while fewer than two verified copies exist, across all tested failure injections.
- **SC-004**: Peer replication of a model traverses the Thunderbolt mesh, not the egress interface, and performs exactly one external pull per acquisition.
- **SC-005**: Load reports ready only after the engine serves; a model that fails to start is never reported ready.
- **SC-006**: Unload releases the model's resident memory, confirmed by node-reported free memory returning to within 2% of its pre-load value.
- **SC-007**: After a node agent restart with a model loaded, the engine still serves and the registry still reports it loaded, with no orphaned process.
- **SC-008**: Measured resident memory is recorded against the estimate for 100% of loads.
- **SC-009**: An externally-killed engine is reflected as not-loaded within one health interval.

## Assumptions

- Feature 000 has shipped: the workspace, images, compose project, and a launchd-managed node agent on each Studio exist, and the MLX toolchain has been installed into a versioned environment on each Studio.
- The engine is bare `mlx_lm.server`, driven directly. No inference wrapper is introduced (Principle I).
- Only MLX-format repos are in scope here. Inspection, conversion, quantisation, validation, and GGUF rejection are feature 002.
- Routing user traffic to a loaded model is feature 003; this feature's load verb is exercised by the control plane and tests only.
- Eviction, LRU, idle-TTL enforcement, and pinning are feature 004. This feature stores `idle_ttl` and placement policy as data but does not act on them.
- Sharded placements are feature 006; only single-node loads are exercised here.
- Auth is not yet feature-complete (feature 007). The admin-versus-non-admin distinction is the interim static admin token of ADR-0004, presented through the auth seam feature 000 declared; feature 007 replaces it without changing these rules.
- Both Studios have 1.8 TB of disk and 256 GB of memory (verified 2026-08-29), so the two-copies rule bounds the roster by the smaller Studio's free disk.
- Integration tests use a model of 1 GB or less so CI can run on a single Mac, per Principle VII.
