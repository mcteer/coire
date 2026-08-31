# ADR-0006: Separate the control VLAN from the Studio data fabric

- **Status**: Accepted
- **Date**: 2026-08-30
- **Deciders**: Dan McTeer
- **Supersedes**: ADR-0002
- **Constitution**: Principles I, III, IV, VI, and VII

## Context

The first topology made a Thunderbolt Bridge chain (`core — edge-a — edge-b`) the preferred
network for every platform flow. That gave core high bandwidth it does not use, made edge-a a
layer-2 transit dependency between core and edge-b, and coupled ordinary control-plane reachability
to experimental RDMA topology. All three hosts already share an isolated Wi-Fi VLAN.

Only the Studios exchange model-scale data or participate in distributed MLX. Core sends control
requests, prompts, and streamed results; it neither stores model weights nor joins a JACCL process
group. The direct Studio link measured 12.6 Gb/s / 0.85 ms, while Wi-Fi measured 0.4 Gb/s /
23–29 ms. Wi-Fi has ample bandwidth for control traffic, subject to latency and reliability
verification.

## Decision

- The isolated Wi-Fi VLAN is the control fabric for all three hosts and also carries internet
  egress. UniFi DNS names identify hosts; raw addresses remain forbidden in configuration.
- A direct Thunderbolt 5/RDMA link connects edge-a and edge-b as a separate, unrouted data fabric.
  Only model replication, interconnect probes, and distributed MLX traffic use it.
- Core has no Thunderbolt data-fabric address and is not included in the JACCL hostfile.
- Node agents and engines listen on the control VLAN as required, with authentication and host
  firewall rules restricting each port to its minimum caller set. Studio replication endpoints also
  listen on the data fabric and are unavailable on the control VLAN.
- Loss of the Studio data fabric disables replication and RDMA-dependent placements but does not
  remove either Studio from the control plane. Single-node placements remain functional.
- Before physical cutover, measure prompt-to-first-token latency, multi-tool agent-loop latency, node
  health reliability, and image-result transfer time on the VLAN. Failure to meet objectives calls
  for wired control networking, not reconnecting core to the RDMA fabric.

## Migration

The implemented bootstrap and registry contracts still encode `mesh_address`, `.mesh` names, the
`192.168.100.0/24` three-host subnet, and mesh-first/fallback behavior. Per Principles III and VII,
they remain the operational truth until a feature spec amends those contracts and supplies the
migration, tests, observability, and runbook changes. Historical feature artifacts are not rewritten.

That migration must at least:

1. replace the overloaded mesh/egress address model with distinct control and optional data-fabric
   endpoints;
2. move node registration, health, gateway, telemetry, and run callbacks to UniFi DNS names;
3. limit data-fabric addressing and export routes to the Studio pair;
4. update engine binding and firewall rules without widening exposure;
5. update generated OpenAPI/types, contract and integration tests, dashboards, alerts, deployment
   scripts, and runbooks; and
6. document a reversible cable and configuration cutover.

## Consequences

- Core no longer consumes a Thunderbolt port or depends on edge-a as a bridge.
- RDMA changes cannot directly disrupt the control network.
- Wi-Fi availability becomes a control-plane dependency and must be monitored accordingly.
- Bulk model traffic remains isolated from ordinary platform requests.
- ADR-0002 and its managed three-host `.mesh` name scheme remain relevant only to the pre-migration
  implementation.

## Alternatives rejected

- **Keep the three-host Thunderbolt chain** — supplies unused bandwidth to core and retains a transit
  dependency.
- **Carry replication over Wi-Fi** — measured throughput turns a roughly 3.2-minute 300 GB peer copy
  into roughly 104 minutes.
- **Add 10GbE immediately** — no current control-plane flow justifies it; reconsider only if Wi-Fi
  misses measured objectives or the topology gains a non-Thunderbolt model worker.
