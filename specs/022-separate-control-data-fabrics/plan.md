# Implementation Plan: Separate Control and Data Fabrics

**Branch**: `feat/022-separate-control-data-fabrics` | **Date**: 2026-08-30 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/022-separate-control-data-fabrics/spec.md`

## Summary

Replace the implemented three-host Thunderbolt mesh with two explicit network concerns: the existing
isolated Wi-Fi VLAN becomes the primary control fabric for core and both Studios, while a direct
edge-a/edge-b Thunderbolt link remains the data fabric for replication and distributed MLX. Introduce
typed, versioned endpoint contracts; roll the API before the agents for compatibility; split the
current mesh client into purpose-specific control and data clients; bind engines to the protected
control endpoint and exports only to the data endpoint; then remove core from the Thunderbolt hosts
file and cable path after preflight succeeds.

## Technical Context

**Language/Version**: Python 3.13; Bash for managed host/network deployment scripts; YAML for compose,
launchd inventory, alerts, and OpenAPI contract fixtures.

**Primary Dependencies**: Existing FastAPI, Pydantic v2, httpx, SQLAlchemy 2 async, Alembic,
OpenTelemetry, Prometheus client, MLX/mlx-lm/JACCL. No new dependency.

**Storage**: Postgres 17 node rows gain versioned endpoint data and path observations; managed Studio
data-fabric names remain in `deploy/cluster/hosts`; no model or platform truth moves off core.

**Testing**: pytest unit and contract tests; generated OpenAPI freshness check; simulated control and
data networks in compose integration; real-cluster manual validation for VLAN, firewall, cable,
replication, single-node inference, JACCL, failure injection, and rollback.

**Target Platform**: Core containers on Apple Silicon via OrbStack; native macOS 26.2+ node agents on
two M3 Ultra Studios; isolated UniFi Wi-Fi VLAN plus direct Thunderbolt 5 between the Studios.

**Project Type**: Python workspace monorepo spanning shared contracts, control-plane API, node agent,
deployment, observability, and operational documentation.

**Performance Goals**: Control health probes p95 ≤ 50 ms; control overhead per tool round trip p95 ≤
100 ms; loaded tiny-model first token p95 ≤ 1.5 s for prompts ≤ 4k tokens; Studio replication remains
on the measured high-bandwidth link and never uses the VLAN.

**Constraints**: Core hosts no model and has no data-fabric endpoint; only bare engines; no raw host
addresses in production configuration; deny-by-default listener and firewall policy; replication
fails closed; rolling migration and rollback preserve existing state; no dependency or network-policy
widening.

**Scale/Scope**: Exactly one core and two Studios; two control paths and one data link; existing node,
engine, model-copy, acquisition, health, and telemetry surfaces; no public API behavior change.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design; all gates pass.*

| Principle / constraint | Verdict | Evidence |
|---|---|---|
| **I. Bare engines, owned lifecycle** | PASS | Engine command construction remains in coire-node. Only bind address and permitted callers change; no wrapper or public engine exposure is introduced. |
| **II. Control node disposable, workers sacred** | PASS | Core remains control-only; Studios retain engines and caches only. Network separation adds no role to either host. |
| **II-a. One service, one container, bare image** | PASS | No service, image, container role, capability, or runtime package is added. |
| **III. Contracts first, typed end to end** | PASS | `NodeEndpointSet`, endpoint versioning, path kinds, and link status are designed in `data-model.md` and `contracts/network-api.yaml` before implementation. A dual-read migration provides the compatibility note; OpenAPI/types and contract tests are explicit tasks. |
| **IV. Zero implicit trust** | PASS | Existing authentication remains mandatory. Host firewall matrices narrow engine and export access; forbidden cross-fabric attempts are recorded. No CORS or network is widened. |
| **V. Models are data; only admins acquire** | PASS | Replication remains grant-scoped, Studio-to-Studio, and fail-closed. Registry ids and verified-copy rules are unchanged. |
| **VI. Observable or it doesn't ship** | PASS | Separate control/data path metrics, spans, logs, dashboard panels, and alert rules are in the design and acceptance guide. |
| **VII. Spec-driven, test-gated** | PASS | Feature 022 owns the migration; legacy specs carry supersession notes. Unit, contract, simulated integration, tiny-model, and documented real-cluster validation are required. |
| **LAN and naming constraints** | PASS | UniFi DNS names the control endpoints; the Studio-only unrouted fabric uses managed names rather than raw addresses outside its single deployment mapping. |
| **Latency and recovery standards** | PASS | Preflight gates on the existing p95 objectives. Rolling dual-read contracts and a reversible physical cutover preserve recoverability. |

**Post-design re-check**: The versioned registration contract returns a response matching the caller's
contract version, avoiding additive fields that an old `extra="forbid"` client would reject. The API
is upgraded first, then each agent; legacy fields remain readable for one release and are not removed
by this feature. Data endpoints never appear in user-facing model listings. All gates remain PASS.

## Project Structure

### Documentation (this feature)

```text
specs/022-separate-control-data-fabrics/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── network-api.yaml
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
packages/coire-core/src/coire_core/
├── models/node.py          # endpoint contract versions, NodeEndpointSet, NetworkPath
├── models/link.py          # ControlPathStatus and StudioDataLinkStatus
├── net.py                  # ControlClient and DataFabricClient; no implicit cross-fabric fallback
└── settings.py             # control/data naming and listener settings

apps/coire-api/src/coire_api/
├── db.py                   # endpoint persistence migration
├── nodes_client.py         # control-only node calls
├── routes/nodes.py         # v1/v2 registration compatibility
└── telemetry.py            # path classification and metrics

apps/coire-api/alembic/versions/
└── 0003_node_endpoints.py   # reversible endpoint columns/backfill

apps/coire-node/src/coire_node/
├── agent.py                # control listener plus Studio-only data listener
├── register.py             # v2 endpoint registration with legacy rollback mode
├── engines.py              # control bind; local/core caller policy
├── routes/export.py        # data listener only
├── net.py                  # interface/path validation
├── metrics.py              # separate path measurements
└── otel.py                 # control-fabric exporter endpoint

deploy/
├── cluster/hosts           # edge-a/edge-b data names only
├── cluster/nodes.yaml      # declared control and optional data names
├── cluster/firewall.yaml   # auditable minimum-peer matrix
├── cluster/scripts/        # preflight, apply, verify, rollback
├── compose/compose.yaml    # core services publish/listen on control address where needed
├── compose/compose.override.it.yaml # simulated control + data fabrics
└── launchd/com.coire.node.plist.template

apps/coire-api/tests/{unit,contract}/
apps/coire-node/tests/{unit,contract,engine}/
packages/coire-core/tests/
tests/integration/
docs/runbooks/network-fabrics.md
deploy/observability/{grafana,alerts}/
```

**Structure Decision**: Extend existing shared contracts, API, and node-agent modules. Network path is
a cross-cutting property of existing services, not a new service. Keep deployment scripts under
`deploy/cluster/` and operational instructions under `docs/runbooks/`; do not create a network daemon.

## Complexity Tracking

No constitutional violations or new architectural exceptions are required. The temporary dual-read
registration contract is migration complexity mandated by Principle III and is time-boxed: legacy
fields may be removed only in a later separately specified compatibility break after every deployed
agent reports the new contract version.
