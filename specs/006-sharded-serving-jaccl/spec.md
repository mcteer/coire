# Feature Specification: Sharded Serving over JACCL

**Feature Branch**: `006-sharded-serving-jaccl`

**Roadmap ID**: 004 (Phase 1 — Single-node inference works end to end)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "`sharded:tp` and `sharded:pp` placements launched via `mlx.launch` with a generated hostfile; both-node reservation; coordinated teardown; link probe (JACCL all-reduce + ring fallback) stored on the link record and gating TP; rank-failure semantics (instance `failed`, group teardown, `503 Retry-After`, node `degraded`, re-place as smaller single-node variant if it fits); benchmark harness recording tokens/s per placement."

## Overview

This feature unlocks the models that do not fit on one Studio: 200–480 GB weights served across both nodes over the Thunderbolt RDMA link. It adds tensor-parallel and pipeline-parallel placements launched as one process group, simultaneous both-node reservation, coordinated teardown, and a measured link record that gates whether tensor parallelism is allowed at all. It also adds the benchmark harness that answers the question the architecture flags as open: whether sharding actually beats running the largest single-node model on Studio A.

## Clarifications

### Session 2026-08-29

- Q: What gates a tensor-parallel placement? → A: The measured link record. A probe runs on first boot and after every macOS or engine upgrade, recording bandwidth and latency for an all-reduce plus a fallback path. Tensor parallelism is refused when the RDMA path is down or latency exceeds a configured threshold, falling back to pipeline parallelism, which tolerates a slower link.
- Q: What happens when one rank of a running sharded instance dies? → A: The instance fails as a unit. Every rank is torn down, in-flight requests receive `503` with `Retry-After`, the affected node is marked `degraded`, and the scheduler may re-place the model as a smaller single-node variant if one fits on the survivor. There is no live repartitioning and no automatic re-sharding onto a different topology — with two nodes there is nothing to repartition onto, and predictability beats cleverness at 2 a.m.
- Q: How are both nodes reserved without deadlocking against a competing load? → A: A sharded admission acquires both nodes' capacity as a single atomic decision, ordered consistently so two competing sharded admissions cannot each hold one node. If both cannot be satisfied, neither is reserved and the load is refused.
- Q: Does the heterogeneity of the two Studios get compensated for? → A: No — the engine shards evenly, so the 60-core unit bounds throughput and the honest response is measurement, not weighting. The benchmark records tokens per second per placement so the admin can see when single-node on Studio A wins, and the recorded GPU core counts make the expected ceiling visible.
- Q: Is sharded serving considered production-ready on delivery? → A: No. RDMA over Thunderbolt is treated as beta. Every single-node placement must remain fully functional with the RDMA path down, and pipeline parallelism must remain available as the fallback when tensor parallelism is gated off.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A model too large for one Studio serves across both (Priority: P1)

An admin places a model whose weights exceed a single node's budget as a sharded instance. It launches as one process group across both Studios and serves through the gateway exactly like any other model.

**Why this priority**: This is the only way to run the largest models on this hardware, and it is the feature's reason to exist.

**Independent Test**: Place a model larger than one node's budget as `sharded:tp` and complete a streamed chat request through the gateway.

**Acceptance Scenarios**:

1. **Given** a model exceeding one node's budget and a healthy link, **When** an admin places it as sharded, **Then** the instance launches across both nodes and reaches `ready`.
2. **Given** that ready sharded instance, **When** a caller requests it through the gateway, **Then** the response streams normally with no caller-visible difference from single-node serving.
3. **Given** a sharded instance, **When** the cluster state is read, **Then** it shows both nodes, each rank, and one reservation per node.
4. **Given** a sharded instance, **When** it is unloaded, **Then** the whole process group is torn down on both nodes and both reservations are released.

---

### User Story 2 - A rank failure is clean and predictable (Priority: P1)

One node of a running sharded instance dies mid-stream. Callers get an immediate, correct refusal rather than a hang, the group is torn down, and the platform re-places the model as a single-node instance if one fits.

**Why this priority**: This is the roadmap's named acceptance bar, and an unclean rank failure leaves half a process group holding 100+ GB on a node the ledger believes is free.

**Independent Test**: Kill rank 1 during a stream and confirm a clean `503`, complete teardown, and a re-placed instance within the TTL.

**Acceptance Scenarios**:

1. **Given** a streaming request against a sharded instance, **When** rank 1 is killed, **Then** the caller receives `503` with `Retry-After` promptly rather than a truncated or hanging stream.
2. **Given** that failure, **When** teardown runs, **Then** no engine process remains on either node and both reservations are released.
3. **Given** that failure, **When** the node's state is read, **Then** it is marked `degraded`.
4. **Given** a smaller variant that fits the surviving Studio, **When** re-placement runs, **Then** a single-node instance of that variant becomes ready within the configured TTL.
5. **Given** no variant fits the survivor, **When** re-placement is attempted, **Then** the model remains unavailable, an alert condition is raised, and the platform waits for the node rather than thrashing.

---

### User Story 3 - The link is measured, not assumed (Priority: P2)

The platform probes the interconnect and records what it actually measured, and refuses tensor parallelism when the link cannot support it.

**Why this priority**: Sharding onto a degraded link produces something that appears to work and performs terribly. Gating on measurement is what makes the beta status of RDMA survivable.

**Independent Test**: Run the probe, confirm bandwidth and latency are recorded, then simulate a degraded link and confirm tensor parallelism is refused while pipeline parallelism remains available.

**Acceptance Scenarios**:

1. **Given** a healthy mesh, **When** the probe runs, **Then** measured bandwidth and latency are recorded on the link record with a timestamp.
2. **Given** a link whose latency exceeds the threshold, **When** a tensor-parallel placement is requested, **Then** it is refused with the measured figures, and pipeline parallelism is offered.
3. **Given** an RDMA path that is down, **When** any sharded placement is requested, **Then** tensor parallelism is refused and pipeline parallelism is attempted over the fallback path.
4. **Given** a macOS or engine upgrade on a node, **When** the node returns, **Then** the probe re-runs before that node is eligible for tensor parallelism.

---

### User Story 4 - The admin can tell whether sharding is worth it (Priority: P2)

A benchmark records throughput per placement for a model, so the choice between single-node on Studio A, tensor parallel, and pipeline parallel is made from numbers.

**Why this priority**: The architecture flags heterogeneous Studios as a real risk and this benchmark is the named mitigation. It informs which models to publish but does not block serving.

**Independent Test**: Benchmark one mid-size model across all three placements and read the comparison.

**Acceptance Scenarios**:

1. **Given** a model that fits all three placements, **When** the benchmark runs, **Then** tokens per second is recorded for single-node-A, tensor parallel, and pipeline parallel.
2. **Given** benchmark results, **When** the admin views the model, **Then** the placements are shown side by side with the measurement conditions.
3. **Given** a repeated benchmark, **When** it runs again, **Then** results are stored as a new record rather than overwriting, so regressions after an upgrade are visible.

---

### Edge Cases

- One node lacks capacity for its half while the other has it: the sharded admission MUST reserve neither and MUST refuse with both nodes' figures.
- Two sharded admissions compete: ordering MUST prevent each holding one node; exactly one MUST proceed.
- The launch succeeds on rank 0 but fails on rank 1: rank 0 MUST be torn down rather than left serving a broken group.
- A node reboots while holding a sharded rank: the instance MUST fail as a unit rather than the survivor being adopted as a working instance.
- The link probe itself fails to run: tensor parallelism MUST be refused, since an unmeasured link is not a healthy link.
- A sharded instance is drained normally: both ranks MUST stop together, and no rank may outlive the group.
- Idle TTL expires on a sharded instance: teardown MUST be coordinated across both nodes and both reservations released together.
- An admin requests tensor parallelism for an architecture that does not support it: it MUST be refused at placement time with the architecture named, not discovered at launch.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST support `sharded:tp` and `sharded:pp` placements launched as a single process group spanning both Studios.
- **FR-002**: Launching a sharded instance MUST use a generated hostfile derived from the declared node inventory, never hand-maintained configuration.
- **FR-003**: A sharded admission MUST reserve capacity on both nodes as one atomic decision; if either cannot be satisfied, neither MUST be reserved.
- **FR-004**: Competing sharded admissions MUST be ordered so that two cannot each hold one node.
- **FR-005**: Teardown of a sharded instance MUST stop every rank on both nodes and release both reservations together.
- **FR-006**: No rank may outlive its group; a partially-launched group MUST be torn down completely.
- **FR-007**: The system MUST probe the interconnect on first boot and after every macOS or engine upgrade, recording measured bandwidth and latency on a link record.
- **FR-008**: Tensor parallelism MUST be refused when the RDMA path is down, when measured latency exceeds the configured threshold, or when no probe result exists.
- **FR-009**: Pipeline parallelism MUST remain available when tensor parallelism is gated off.
- **FR-010**: A placement request for an architecture that does not support the requested parallelism MUST be refused at placement time, naming the architecture.
- **FR-011**: Failure of any rank MUST move the instance to `failed` and tear down the whole group.
- **FR-012**: In-flight requests against a failed sharded instance MUST receive `503` with `Retry-After` promptly.
- **FR-013**: A node whose rank failed MUST be marked `degraded`.
- **FR-014**: After a rank failure the scheduler MUST attempt to re-place the model as a single-node instance of a smaller variant when one fits the surviving node.
- **FR-015**: When no variant fits the survivor, the model MUST remain unavailable and an alert condition MUST be raised; the system MUST NOT repeatedly retry a placement that cannot fit.
- **FR-016**: The system MUST NOT repartition a running instance and MUST NOT automatically re-shard onto a different topology.
- **FR-017**: Single-node placements MUST remain fully functional when the RDMA path is unavailable.
- **FR-018**: A benchmark MUST record tokens per second per placement for a model, storing each run as a new record with its conditions.
- **FR-019**: The recorded per-node GPU core counts MUST be visible so the expected tensor-parallel ceiling is apparent.
- **FR-020**: Link measurements and placement refusals MUST be observable to the operator with their measured figures.
- **FR-021**: Link state transitions MUST be damped: a link MUST NOT be marked down or restored on a single observation, and recovery MUST require a longer confirmation than failure, so a flapping link does not repeatedly tear down and re-place instances.
- **FR-022**: A flapping link MUST be detectable and alertable as a condition distinct from a clean link failure.

### Key Entities

- **Sharded Instance**: A model instance spanning two nodes. Model variant, parallelism mode, ranks with node, port and process identity, group identity, reservations on both nodes, state.
- **Link Record**: The measured interconnect between two nodes. Node pair, transport, measured bandwidth, measured latency, probe outcome, measured-at, eligibility verdict for tensor parallelism.
- **Benchmark Result**: One measured comparison. Model variant, placement, tokens per second, prompt and generation conditions, node GPU core counts, run-at.
- **Degraded Node**: A node marked unhealthy after a rank failure. Node, reason, marked-at, cleared-at.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A model larger than 250 GB serves successfully through the gateway as a sharded instance.
- **SC-002**: Killing rank 1 mid-stream yields a `503` with `Retry-After` and leaves no engine process on either node.
- **SC-003**: After a rank failure a re-placed single-node instance becomes ready within the configured TTL whenever a variant fits the survivor.
- **SC-004**: A benchmark report compares single-node-A, tensor parallel, and pipeline parallel for a mid-size model with tokens per second for each.
- **SC-005**: Measured link bandwidth and latency are recorded and visible for the node pair.
- **SC-006**: Tensor parallelism is refused whenever the link is down, degraded beyond threshold, or unmeasured, in 100% of trials.
- **SC-007**: All single-node serving continues to function with the RDMA path disabled.
- **SC-008**: A sharded admission never leaves a reservation held on one node when the other could not be satisfied.

## Assumptions

- Features 001–005 have shipped, in particular the instance state machine, which this feature extends to multi-rank instances rather than replacing.
- Verified 2026-08-29: both Studios are M3 Ultra with 256 GB, running macOS 26.6.2, above the 26.2 floor that RDMA over Thunderbolt requires. Feature 022 supersedes the original three-host cabling assumption: the data fabric is a direct Studio A to Studio B link, and core is absent from the JACCL topology.
- Not yet done at spec time: RDMA enablement per machine and generation of the JACCL hostfile. Both are roadmap 000a manual prerequisites that must be complete before this feature can be verified on the real cluster.
- Studio A is rank 0 for every sharded run; Studio B is rank 1. GPU core counts are 80 and 60 respectively, so tensor-parallel throughput is bounded by the 60-core unit.
- Sharded serving is treated as beta throughout; the platform must remain fully useful with it disabled.
- The engine supports tensor parallelism by default and pipeline parallelism by option when launched as a distributed group; Coire owns the launch, never a wrapper.
- Benchmarking is a manual admin-triggered action, not a scheduled job.
- Per Principle VII this feature requires a documented manual verification on the real cluster before merge; CI cannot exercise two nodes.
