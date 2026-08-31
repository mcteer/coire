# Feature Specification: Separate Control and Data Fabrics

**Feature Branch**: `feat/022-separate-control-data-fabrics`

**Created**: 2026-08-30

**Status**: Draft

**Input**: User description: "Use the existing isolated Wi-Fi VLAN for control-plane traffic among core, edge-a, and edge-b; reserve a direct Thunderbolt/RDMA connection for Studio-to-Studio model replication and distributed MLX; remove core from the Thunderbolt fabric."

## Clarifications

### Session 2026-08-30

- Q: Does core retain any Thunderbolt or RDMA role? → A: No. Core is reachable on the isolated control VLAN and is absent from the Studio data fabric and distributed hostfiles.
- Q: Which traffic remains on Thunderbolt? → A: Model replication, interconnect probes, and distributed MLX traffic between edge-a and edge-b only.
- Q: Is Wi-Fi performance assumed sufficient without verification? → A: No. Control latency is measured and recorded, while workload-level reliability, tool-loop, and first-token objectives determine acceptance.

### Session 2026-08-31

- Q: Must authenticated health probes remain below 50 ms p95? → A: No. All 200 probes must succeed without degraded-path events, and latency is recorded rather than gated by a standalone ceiling.
- Q: May the control fabric move to a wired connection? → A: No. Control and public egress remain on Wi-Fi for all three hosts; Thunderbolt remains the only wired connection and carries Studio-to-Studio data traffic only.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Operate the cluster without core on Thunderbolt (Priority: P1)

As an operator, I can manage both Studios from core over the isolated control VLAN so that ordinary
cluster operation does not depend on a Thunderbolt bridge or an intermediate Studio.

**Why this priority**: This removes the unnecessary physical dependency and establishes the network
path required by every control-plane workflow.

**Independent Test**: Disconnect every Thunderbolt cable from core, retain only the direct Studio
link, restart all three hosts, and verify that both Studios register, remain healthy, and serve
single-node inference through the gateway.

**Acceptance Scenarios**:

1. **Given** all three hosts are on the isolated VLAN and core has no Thunderbolt connection, **When** both node agents start, **Then** each registers with core and remains healthy without degraded-path warnings.
2. **Given** edge-a is unavailable, **When** core routes a request to a healthy single-node instance on edge-b, **Then** the request succeeds without traversing edge-a.
3. **Given** a control request is sent from outside its permitted peer set, **When** it reaches a protected listener, **Then** it is refused and the refusal is observable without exposing engine details.

---

### User Story 2 - Preserve the fast Studio data path (Priority: P2)

As an operator, I can replicate models and run distributed placements over a dedicated direct link
between the Studios so that bulk and collective traffic remains fast and isolated from control
traffic.

**Why this priority**: The direct link is still necessary for model-scale transfers and JACCL, but
those workloads do not require core to join the fabric.

**Independent Test**: Replicate a model between the Studios and run the two-rank link probe while
capturing interface counters; the data-fabric counters increase by the transferred bytes and the
control VLAN does not carry model payloads.

**Acceptance Scenarios**:

1. **Given** both Studios have a healthy direct data link, **When** a model is replicated, **Then** the model payload crosses only that link and the verified-copy rules remain unchanged.
2. **Given** a valid two-rank distributed placement, **When** it launches, **Then** its host inventory contains edge-a and edge-b and contains no core entry.
3. **Given** the direct data link is unavailable, **When** an operator requests a single-node placement, **Then** it remains functional over the control fabric.
4. **Given** the direct data link is unavailable, **When** replication or an RDMA-dependent placement is requested, **Then** it fails closed with a specific observable reason rather than moving model payloads onto Wi-Fi.

---

### User Story 3 - Cut over safely and reversibly (Priority: P3)

As an operator, I can validate and migrate the cluster without an extended outage, with clear
rollback instructions if Wi-Fi latency, reliability, naming, or firewall policy is unsuitable.

**Why this priority**: The existing software encodes the old three-host mesh, so the physical cable
change must follow rather than precede a contract-compatible software migration.

**Independent Test**: Follow the migration runbook on the real cluster, exercise its preflight,
cutover, verification, and rollback sections, and record the required latency and reliability data.

**Acceptance Scenarios**:

1. **Given** the old topology is still active, **When** the operator runs preflight validation, **Then** no cable change is requested until control names, listener restrictions, and performance gates pass.
2. **Given** the new topology misses a required service objective, **When** rollback is initiated, **Then** the documented prior configuration can be restored without loss of registry or job state.
3. **Given** cutover succeeds, **When** the operator views cluster telemetry, **Then** control-path health and Studio data-link health are shown as separate concerns with actionable alerts.

### Edge Cases

- Wi-Fi remains associated but cannot reach the VLAN gateway or core.
- UniFi DNS returns a stale or unexpected address after a DHCP reservation changes.
- The Thunderbolt link is healthy for ordinary IP transfer but unavailable to RDMA.
- A model replication begins immediately before the data link fails.
- One Studio restarts during cutover while the other still uses the former endpoint contract.
- A caller attempts to use a control address for model export or a data-fabric address for a control route.
- The VLAN meets bandwidth requirements but sustained jitter causes tool-heavy agent loops to miss latency targets.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The platform MUST treat the isolated VLAN as the primary control fabric shared by core, edge-a, and edge-b.
- **FR-002**: Core MUST register, monitor, schedule, route to, and manage each Studio without a Thunderbolt address or a path through the other Studio.
- **FR-003**: Node endpoint contracts MUST distinguish a required control endpoint from an optional data-fabric endpoint and MUST reject invalid role/address combinations.
- **FR-004**: Control endpoints MUST be identified by stable UniFi DNS names in configuration; raw host addresses MUST NOT appear in production configuration.
- **FR-005**: Node registration, health, scheduler commands, gateway-to-engine traffic, telemetry export, user-run callbacks, and ordinary result transfer MUST use the control fabric.
- **FR-006**: The direct Thunderbolt data fabric MUST contain only edge-a and edge-b; core MUST have no endpoint on it.
- **FR-007**: Model payload replication MUST use only the Studio data fabric and MUST fail closed rather than fall back to the control VLAN.
- **FR-008**: Distributed MLX host inventories and interconnect probes MUST contain exactly the participating Studios and MUST never contain core.
- **FR-009**: Loss of the Studio data fabric MUST NOT prevent node registration, health reporting, control operations, or single-node inference on either otherwise healthy Studio.
- **FR-010**: Loss of a Studio's control path MUST mark that node unreachable even if its data-fabric peer remains reachable.
- **FR-011**: Every listener MUST remain authenticated and MUST be restricted to its minimum caller set; the migration MUST NOT widen engine, node-agent, replication, telemetry, database, or container-network exposure.
- **FR-012**: The platform MUST expose separate health and telemetry for each node's control path and for the Studio-to-Studio data link.
- **FR-013**: Control-path loss, data-link loss, and forbidden cross-fabric traffic MUST each produce structured logs, metrics, traces where applicable, and an actionable alert; control-path latency MUST remain measured and visible for workload diagnosis.
- **FR-014**: Migration MUST preserve existing node identities, registry state, model-copy state, engine lifecycle state, job history, and audit history.
- **FR-015**: The public and administrative API compatibility surface MUST remain unchanged except for additive, prefixed Coire fields where endpoint details are exposed.
- **FR-016**: Contract changes MUST include a compatibility note and coordinated updates to generated API artifacts and contract tests.
- **FR-017**: A preflight MUST prove DNS correctness, permitted connectivity, forbidden connectivity, control latency, agent-loop latency, and result-transfer behavior before physical cutover.
- **FR-018**: The migration MUST provide documented rollback that restores the prior working network configuration without deleting platform data or model copies.
- **FR-019**: Historical specifications for the previous topology MUST identify the requirements superseded by this feature without rewriting their completed implementation record.
- **FR-020**: No model weights, datasets, generated images, secrets, or caller-provided engine paths may be introduced into repository configuration or test fixtures by this migration.

### Key Entities

- **Node Endpoint Set**: The addresses by which one declared node participates in the cluster, containing one required control endpoint and, for Studios only, an optional data-fabric endpoint.
- **Control Path Status**: The measured reachability, latency, last successful observation, and failure reason between core and a Studio on the VLAN.
- **Data Link Status**: The measured IP and RDMA capability, bandwidth, latency, last probe, and failure reason for the direct edge-a/edge-b link.
- **Network Path**: A classified path (`control` or `data`) attached to relevant requests, measurements, logs, and traces so cross-fabric use can be detected.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With core disconnected from Thunderbolt, both Studios automatically register and become healthy within two minutes of a simultaneous cold start.
- **SC-002**: Two hundred consecutive authenticated health probes to each Studio over the control VLAN succeed with no degraded-path events; p50, p95, p99, and maximum round-trip latency are recorded as diagnostic evidence rather than pass/fail criteria.
- **SC-003**: Loaded single-node inference through the gateway retains the architecture target of at most 1.5 seconds p95 to first token for prompts of at most 4,000 tokens on the tiny validation model.
- **SC-004**: The multi-tool validation scenario completes without network errors and with no more than 100 ms p95 control-network overhead per tool round trip.
- **SC-005**: A representative image result reaches core successfully over the control VLAN, with transfer time recorded and no payload bytes retained on the Studio after completion.
- **SC-006**: A model replication increases only the direct Studio-link payload counters by approximately the model size; control-VLAN payload counters do not show a model-scale transfer.
- **SC-007**: Disconnecting the Studio data link blocks replication and tensor-parallel admission within the existing probe threshold while both Studios remain independently manageable and capable of single-node serving.
- **SC-008**: Firewall verification proves every documented allowed flow succeeds and every forbidden peer/path combination is rejected.
- **SC-009**: The migration and rollback procedures each complete on the real cluster without loss or mutation of registry records, verified model copies, job history, or audit history.
- **SC-010**: Dashboards distinguish the two Studio control paths from the one Studio data link, and deliberate failure of each path fires its corresponding alert.

## Assumptions

- All three hosts already join a UniFi-managed isolated Wi-Fi VLAN with deny-by-default access to other VLANs.
- UniFi can provide stable DNS names backed by reserved addresses for all three hosts.
- The direct edge-a/edge-b Thunderbolt link remains physically available and supports ordinary IP transfer independently of whether RDMA is enabled.
- The initial migration keeps the existing authentication model and does not accelerate features 007 or 019.
- Control and public internet egress remain on Wi-Fi for all three hosts. The direct Studio-to-Studio Thunderbolt connection is the only wired connection in the architecture.
- Implementing distributed serving itself remains in feature 006; this feature supplies its corrected two-Studio topology and endpoint contracts.

## Dependencies and Supersession

- Supersedes the network-path requirements FR-013 through FR-013c in `specs/000-bootstrap/spec.md`.
- Supersedes the engine-binding portion of FR-018 in `specs/001-model-registry-node-agent/spec.md`; its direct peer-replication requirements remain in force.
- Corrects the physical-topology assumptions in `specs/006-sharded-serving-jaccl/spec.md` without changing its two-rank placement behavior.
- Clarifies `specs/019-upgrades-rollback/spec.md` so RDMA revalidation applies only to the Studio pair.
- Implements the decision recorded in `docs/adr/0006-separate-control-and-studio-data-fabrics.md`.

## Out of Scope

- Adding a model worker beyond edge-a and edge-b.
- Replacing Wi-Fi with Ethernet, 10GbE, Thunderbolt, or any other wired control or egress fabric.
- Changing public inference, chat, image, training, or agent behavior unrelated to network routing.
- Adding a database, web tier, model engine, or user harness to a host where the constitution forbids it.
- Making replication fall back to the control VLAN.
