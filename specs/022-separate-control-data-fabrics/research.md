# Research: Separate Control and Data Fabrics

**Feature**: `022-separate-control-data-fabrics` · **Date**: 2026-08-30

## R1. Fabric responsibilities

**Decision**: Use the isolated UniFi Wi-Fi VLAN for all three-host control and egress traffic. Use the
direct edge-a/edge-b Thunderbolt link only for model replication, link probes, and distributed MLX.

**Rationale**: Core neither stores weights nor joins distributed process groups. Control payloads are
small; the measured 12.6 Gb/s direct link remains available where bandwidth matters. Removing core
also eliminates edge-a as a bridge dependency.

**Alternatives considered**: Keep the chain (unnecessary dependency); put all traffic on Wi-Fi
(unacceptable model-copy time); add 10GbE now (no measured need).

## R2. Naming

**Decision**: Use UniFi DNS FQDNs (`coire-core.lab`, `coire-edge-a.lab`, `coire-edge-b.lab`) on the control VLAN.
Use managed `.fabric` names for the two static, unrouted Studio data endpoints; only
`deploy/cluster/hosts` maps those names to addresses.

**Rationale**: This obeys the constitution's hostname rule, removes nondeterministic mDNS, and keeps
the no-DNS data link operable when core is down.

**Alternatives considered**: `.local` mDNS (already measured nondeterministic); raw addresses in
service config (constitution violation); DNS hosted on core for the data link (unavailable with core).

**Rollout amendment (2026-08-31)**: Direct queries to the UniFi resolver proved that all three
`.lab` FQDNs resolve consistently while the bare `coire-edge-b` name returns `NXDOMAIN`. The v2
contract temporarily accepts both forms for rolling compatibility, but production inventory and
runtime configuration use only the FQDNs.

## R3. Rolling contract compatibility

**Decision**: Add a versioned endpoint registration shape. The API accepts legacy and v2 requests and
responds in the same version the caller used. Upgrade API first, then agents one at a time. Retain
legacy persistence/read support for one release; removal is outside this feature.

**Rationale**: Old Pydantic wire models forbid extra fields, so an unversioned additive response can
break rollback. Request-matched responses permit mixed versions during cutover.

**Alternatives considered**: Atomic three-host deployment (fragile); reinterpret `mesh_address` as
Wi-Fi (misleading and unsafe); immediate field removal (breaking contract without rollback).

## R4. Listener policy

**Decision**: Serve node control routes and engines on the control endpoint. Serve replication export
routes only on the Studio data endpoint. Authenticate all routes as today and apply a generated host
firewall matrix: core→node agent/engine, Studio→peer export, run containers→core API, and nothing more.

**Rationale**: An unrouted mesh previously supplied reachability restriction implicitly. Moving to a
routed VLAN requires an explicit peer restriction to preserve Principle IV.

**Alternatives considered**: Authentication without firewalling (unnecessary exposure); proxy all
engines through the node agent (larger lifecycle change, outside this migration).

## R5. No fallback between fabrics

**Decision**: Replace generic mesh-first fallback with clients whose purpose fixes their path.
`ControlClient` uses only control DNS. `DataFabricClient` uses only `.fabric` names and raises a typed
unreachable error; replication never falls back.

**Rationale**: The former fallback expressed a topology failure, not a product need. Silent or even
alerted cross-fabric transfer can move hundreds of gigabytes over Wi-Fi and obscures failure domains.

**Alternatives considered**: Keep fallback for control (there is no second control path after core
leaves Thunderbolt); allow slow replication fallback (violates the accepted architecture).

## R6. Performance gate

**Decision**: Measure 200 health probes per Studio and record their latency distribution without a
standalone latency ceiling. Gate acceptance on probe reliability plus tiny-model first-token latency,
a representative multi-tool loop, and image-result transfer under SC-002 through SC-005. Control and
public egress remain on Wi-Fi; the direct Studio Thunderbolt link remains data-only.

**Rationale**: Existing Wi-Fi measurements show sufficient bandwidth but relatively high latency and
jitter; workload-level evidence is required.

**Alternatives considered**: Assume streaming hides all latency (tool loops still amplify it); use a
health-probe latency ceiling as a proxy for user-visible behavior (rejected after real-cluster
measurement); add a wired control fabric (outside the intended architecture).

## R7. Observability model

**Decision**: Record `network_path=control|data`, `peer`, and relevant node/model identifiers on
network spans and logs. Export separate control reachability/latency and data-link IP/RDMA metrics.
Alert on node control loss, data-link loss, forbidden path use, and sustained latency breaches.

**Rationale**: The two fabrics have different availability meanings and remedies; a single “mesh
down” signal becomes ambiguous after separation.

**Alternatives considered**: Infer path from addresses in dashboards (brittle); reuse the fallback
counter (wrong semantic).

## R8. Physical migration and rollback

**Decision**: Deploy dual-contract software and firewall policy, pass preflight, stop active
replication/sharded jobs, remove core's managed mesh binding and cable, verify, then enable the new
alerts. Rollback restores the managed files/cable and selects legacy agent registration; it never
rolls back the database migration destructively.

**Rationale**: Schema expansion is safe to leave in place, while physical and listener changes need a
deterministic inverse. No model or registry data participates in rollback.

**Alternatives considered**: Cable first (current software requires the mesh); destructive database
downgrade during incident response (unnecessary risk).
