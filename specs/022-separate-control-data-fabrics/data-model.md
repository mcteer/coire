# Data Model: Separate Control and Data Fabrics

**Feature**: `022-separate-control-data-fabrics` · **Date**: 2026-08-30

All wire shapes live in `packages/coire-core`, use Pydantic v2, and set
`model_config = ConfigDict(extra="forbid")`.

## NetworkPath

Enum: `control | data`. Replaces the old response-level `mesh | fallback` meaning for v2 callers.
It classifies the intended fabric; it does not permit automatic fallback.

## NodeEndpointSet

| Field | Type | Constraints | Meaning |
|---|---|---|---|
| `contract_version` | literal `2` | required | Selects the new registration shape |
| `control_host` | hostname | required; equals declared inventory name | Stable UniFi DNS endpoint |
| `data_host` | hostname or null | required | `.fabric` name; required for Studio role, forbidden for core |

Addresses are resolved and observed at runtime but are not accepted as configuration identity. A
registration whose host names do not match declared inventory is rejected.

## NodeRegistrationV2

Retains node identity, token, capacity, GPU count, and agent version from v1, and replaces
`mesh_address`/`egress_address` with `endpoints: NodeEndpointSet`.

Validation:

- name must be declared and match `control_host`;
- only declared Studios may provide `data_host`;
- both Studios must have distinct control hosts and distinct data hosts;
- core never registers as a worker and has no data host;
- token validation remains unchanged.

The API accepts `NodeRegistrationV1 | NodeRegistrationV2`. It returns the corresponding `NodeV1` or
`NodeV2`; it never adds v2 fields to a v1 response.

## NodeV2

Persisted node identity plus `endpoints`, capacity, agent version, timestamps, and reachability.
During migration the database retains nullable legacy address columns and new endpoint columns.
Backfill does not reinterpret old mesh addresses as control endpoints; v2 values arrive through a v2
registration.

## NodeStatusV2

Retains resource, engine, and job status fields. `path` becomes `control` and the response adds no
data-link status: the Studio pair's link is a separate entity rather than a property of one health
request.

## ControlPathStatus

| Field | Type | Constraints |
|---|---|---|
| `node_name` | hostname | one of the two declared Studios |
| `state` | `unknown \| healthy \| degraded \| unreachable` | damped transitions |
| `latency_ms` | float or null | non-negative |
| `consecutive_successes` | int | non-negative |
| `consecutive_failures` | int | non-negative |
| `last_success_at` | datetime or null | UTC |
| `last_failure_at` | datetime or null | UTC |
| `reason` | string or null | bounded, client-safe |

Transitions preserve the existing health damping. Data-link observations never make a control path
healthy.

## StudioDataLinkStatus

One logical link keyed by the unordered node pair `{coire-edge-a, coire-edge-b}`.

| Field | Type | Constraints |
|---|---|---|
| `node_a`, `node_b` | hostname | distinct Studios; canonical order |
| `ip_state` | `unknown \| up \| down` | damped |
| `rdma_state` | `unknown \| up \| degraded \| down` | measured independently |
| `bandwidth_bytes_per_second` | int or null | positive when present |
| `latency_ms` | float or null | non-negative |
| `measured_at` | datetime or null | UTC |
| `reason` | string or null | bounded, client-safe |

Replication requires `ip_state=up`. Tensor parallelism additionally requires an acceptable
`rdma_state`, bandwidth, and latency under feature 006 rules. Control reachability does not derive
from this entity.

## Compatibility lifecycle

1. **Legacy**: only v1 fields populated; old clients operate unchanged.
2. **Mixed**: API accepts both; each upgraded Studio populates v2 fields while legacy fields remain.
3. **V2 active**: scheduler and clients use v2 control endpoints only; legacy fields remain for rollback.
4. **Legacy removal**: explicitly outside feature 022 and requires a later spec and migration.

The `0003_node_endpoints` migration is additive and reversible only while no later feature depends on
v2 columns. Operational rollback leaves the columns in place and switches behavior; it does not
discard endpoint observations.
