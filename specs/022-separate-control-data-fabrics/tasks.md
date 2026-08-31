# Tasks: Separate Control and Data Fabrics

**Input**: Design documents from `/specs/022-separate-control-data-fabrics/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/network-api.yaml

**Tests**: Required by Principle VII and by the feature success criteria. Write each listed test before
its corresponding implementation and confirm it fails for the intended reason.

**Organization**: Tasks are grouped by user story so each story has an independently testable result.

## Phase 1: Setup and supersession

**Purpose**: Establish the reviewed contract delta and remove ambiguity in earlier design artifacts.

- [X] T001 Validate `specs/022-separate-control-data-fabrics/contracts/network-api.yaml` as OpenAPI 3.1 and record the command in `specs/022-separate-control-data-fabrics/quickstart.md`
- [X] T002 [P] Add feature 022 and its roadmap ID to the spec directory mapping in `docs/ROADMAP.md`
- [X] T003 [P] Audit supersession references in `specs/000-bootstrap/spec.md`, `specs/001-model-registry-node-agent/spec.md`, `specs/006-sharded-serving-jaccl/spec.md`, and `specs/019-upgrades-rollback/spec.md`
- [X] T004 [P] Reconcile network terminology across `docs/ARCHITECTURE.md`, `docs/adr/0001-defer-auth-and-edge-until-external-traffic.md`, `docs/adr/0002-mesh-name-resolution-via-managed-hosts-file.md`, and `docs/adr/0006-separate-control-and-studio-data-fabrics.md`

---

## Phase 2: Foundational contracts and persistence

**Purpose**: Introduce the compatibility-safe endpoint model shared by every story.

**⚠️ CRITICAL**: No user-story implementation begins until the contracts, migration, and common
clients are complete and green.

- [X] T005 [P] Add failing v1/v2 endpoint-model and role-validation tests in `packages/coire-core/tests/test_models.py`
- [X] T006 [P] Add failing purpose-fixed control/data client tests, including no-cross-fabric fallback, in `packages/coire-core/tests/test_net.py`
- [X] T007 Implement `NetworkPath`, `NodeEndpointSet`, `NodeRegistrationV2`, and version-matched node response types in `packages/coire-core/src/coire_core/models/node.py`
- [X] T008 [P] Implement `ControlPathStatus` and `StudioDataLinkStatus` wire types in `packages/coire-core/src/coire_core/models/link.py` and export them from `packages/coire-core/src/coire_core/models/__init__.py`
- [X] T009 Replace generic mesh/fallback selection with explicit `ControlClient` and `DataFabricClient` behavior while retaining the legacy client during migration in `packages/coire-core/src/coire_core/net.py`
- [X] T010 Add control/data endpoint settings and legacy compatibility settings in `packages/coire-core/src/coire_core/settings.py`
- [X] T011 [P] Add failing forward/downgrade migration tests for nullable endpoint columns in `apps/coire-api/tests/unit/test_migrations.py`
- [X] T012 Add endpoint columns and compatibility metadata to `apps/coire-api/src/coire_api/db.py` and create reversible migration `apps/coire-api/alembic/versions/0003_node_endpoints.py`
- [X] T013 Run `uv run pytest -q packages/coire-core apps/coire-api/tests/unit/test_migrations.py` and make the foundational suite green

**Checkpoint**: Both registration generations can be represented and persisted without changing
runtime routing.

---

## Phase 3: User Story 1 — Operate without core on Thunderbolt (Priority: P1) 🎯 MVP

**Goal**: Move node registration, health, engine access, and telemetry to direct control-VLAN paths.

**Independent Test**: In the simulated topology, core has only the control network, both Studios
register and become healthy, and edge-b remains reachable when edge-a is stopped.

### Tests for User Story 1

- [X] T014 [P] [US1] Add failing version-matched registration contract cases from `contracts/network-api.yaml` in `apps/coire-api/tests/contract/test_register_node.py`
- [X] T015 [P] [US1] Add failing control-listener and forbidden-path contract cases in `apps/coire-node/tests/contract/test_node_health.py`
- [X] T016 [P] [US1] Add failing engine argv tests proving control-address binding in `apps/coire-node/tests/contract/test_node_engines.py`
- [X] T017 [P] [US1] Add failing two-node control-independence integration cases in `tests/integration/test_network_fabrics.py`

### Implementation for User Story 1

- [X] T018 [US1] Accept v1/v2 registrations, validate declared endpoints, persist v2 data, and return the caller's response version in `apps/coire-api/src/coire_api/routes/nodes.py`
- [X] T019 [US1] Route health and engine requests exclusively through `ControlClient` in `apps/coire-api/src/coire_api/nodes_client.py` and `apps/coire-api/src/coire_api/nodes_prober.py`
- [X] T020 [US1] Register v2 control/data host names with an explicit legacy rollback mode in `apps/coire-node/src/coire_node/register.py`
- [X] T021 [US1] Replace mesh/fallback node serving with an authenticated control listener and a separate data listener scaffold in `apps/coire-node/src/coire_node/agent.py` and `apps/coire-node/src/coire_node/__main__.py`
- [X] T022 [US1] Bind bare engines to the control endpoint and preserve local ready-probe/adoption behavior in `apps/coire-node/src/coire_node/engines.py`
- [X] T023 [US1] Send node OTLP telemetry to core by control DNS in `apps/coire-node/src/coire_node/otel.py` and classify control-path spans in `apps/coire-api/src/coire_api/telemetry.py`
- [X] T024 [US1] Replace the simulated three-host mesh with a shared control network plus Studio-only data network in `deploy/compose/compose.override.it.yaml`
- [X] T025 [US1] Update control endpoint variables and listeners in `deploy/compose/compose.yaml` and `deploy/launchd/com.coire.node.plist.template`
- [X] T026 [US1] Generate and enforce the minimum-peer control firewall policy from `deploy/cluster/firewall.yaml` via `deploy/cluster/scripts/apply-firewall.sh`
- [X] T027 [US1] Run `apps/coire-api/tests/contract/test_register_node.py`, `apps/coire-node/tests/contract/test_node_health.py`, `apps/coire-node/tests/contract/test_node_engines.py`, and `tests/integration/test_network_fabrics.py`; verify core has no simulated data-network attachment

**Checkpoint**: Core can operate both Studios with no Thunderbolt path; single-node behavior is green.

---

## Phase 4: User Story 2 — Preserve the fast Studio data path (Priority: P2)

**Goal**: Keep replication and distributed link state exclusively on the direct Studio link.

**Independent Test**: A peer copy increases only simulated/real data-link counters, the export route
is absent from control, and severing data leaves control health and single-node inference usable.

### Tests for User Story 2

- [X] T028 [P] [US2] Add failing export-listener isolation and transfer-grant contract cases in `apps/coire-node/tests/contract/test_node_models.py`
- [X] T029 [P] [US2] Add failing replication no-fallback unit cases in `apps/coire-api/tests/unit/test_reconciler.py`
- [X] T030 [P] [US2] Add failing data-link status contract cases in `apps/coire-api/tests/contract/test_admin_nodes.py`
- [X] T031 [P] [US2] Extend data-link severance and interface-counter assertions in `tests/integration/test_network_fabrics.py`

### Implementation for User Story 2

- [X] T032 [US2] Mount model export routes only on the data listener in `apps/coire-node/src/coire_node/agent.py` and `apps/coire-node/src/coire_node/routes/export.py`
- [X] T033 [US2] Use `DataFabricClient` with `.fabric` peer names and no fallback in `apps/coire-api/src/coire_api/registry/reconciler.py` and node import handling in `apps/coire-node/src/coire_node/worker.py`
- [X] T034 [US2] Record IP and RDMA observations independently in `apps/coire-node/src/coire_node/metrics.py` and expose the typed admin link response in `apps/coire-api/src/coire_api/routes/admin_nodes.py`
- [X] T035 [US2] Reduce `deploy/cluster/hosts` to managed edge-a/edge-b `.fabric` names and add control/data names to `deploy/cluster/nodes.yaml`
- [X] T036 [US2] Create `deploy/cluster/distributed_config.sh` and `deploy/cluster/jaccl-hostfile.template.json` so generated distributed inventory contains exactly edge-a and edge-b
- [X] T037 [US2] Run `apps/coire-node/tests/contract/test_node_models.py`, `apps/coire-api/tests/unit/test_reconciler.py`, `apps/coire-api/tests/contract/test_admin_nodes.py`, and `tests/integration/test_network_fabrics.py`; verify replication fails closed while single-node serving remains green with data disconnected

**Checkpoint**: Bulk and collective traffic is Studio-only and cannot drift onto the control VLAN.

---

## Phase 5: User Story 3 — Cut over safely and reversibly (Priority: P3)

**Goal**: Provide measured preflight, deterministic physical migration, rollback, and distinct
operational signals for both fabrics.

**Independent Test**: Execute preflight, cutover, failure injection, and rollback on the real cluster
without loss of registry, job, audit, engine, or model-copy state.

### Tests for User Story 3

- [X] T038 [P] [US3] Add shell-policy tests for idempotent preflight/apply/rollback targeting in `tests/test_cluster_scripts.py`
- [X] T039 [P] [US3] Add alert-rule unit fixtures for control loss, data loss, forbidden path use, and sustained latency in `tests/test_alert_rules.py`

### Implementation for User Story 3

- [X] T040 [US3] Implement DNS, 200-probe latency, firewall matrix, tiny-model, tool-loop, image-result, and interface-counter gates in `deploy/cluster/scripts/preflight-fabrics.sh`
- [X] T041 [US3] Implement explicit validated apply steps that remove core's managed fabric binding only after preflight in `deploy/cluster/scripts/apply-fabrics.sh`
- [X] T042 [US3] Implement non-destructive legacy listener/hosts restoration in `deploy/cluster/scripts/rollback-fabrics.sh`
- [X] T043 [US3] Add structured `network_path`, `peer`, `node`, and applicable model identifiers plus path-specific counters/spans in `packages/coire-core/src/coire_core/net.py`, `apps/coire-api/src/coire_api/telemetry.py`, and `apps/coire-node/src/coire_node/metrics.py`
- [X] T044 [US3] Add two control-path panels and one data-link panel in `deploy/observability/grafana/dashboards/cluster.json`
- [X] T045 [US3] Add control loss, data loss, forbidden cross-fabric traffic, and latency alerts in `deploy/observability/alerts/network-fabrics.yaml`
- [X] T046 [US3] Write preflight, cutover, failure-injection, rollback, and recovery procedures in `docs/runbooks/network-fabrics.md`
- [ ] T047 [US3] Execute `specs/022-separate-control-data-fabrics/quickstart.md` on the real cluster and record all measured figures and state snapshots in the PR

**Checkpoint**: The physical topology is migrated, observable, and demonstrably reversible.

---

## Phase 6: Polish and release gates

**Purpose**: Complete contract generation, broad regression validation, and documentation consistency.

- [X] T048 Regenerate OpenAPI and `apps/coire-web/src/api/schema.d.ts`, then update freshness fixtures for the additive admin link surface
- [X] T049 [P] Update `docs/runbooks/bootstrap.md` and `docs/runbooks/models.md` from mesh/fallback commands to control/data-fabric operations after cutover
- [X] T050 [P] Create compose and cluster network deployment documentation in `deploy/compose/README.md` and `deploy/cluster/README.md`
- [X] T051 Run the lint, format, type, and unit gates configured by `pyproject.toml`; fix every failure without loosening checks
- [ ] T052 Run the engine/tiny-model validation in `specs/022-separate-control-data-fabrics/quickstart.md` and record its result; do not mark complete on a skip
- [X] T053 Verify images from `deploy/compose/compose.yaml` build and pass `scripts/image-policy.sh`; confirm no service, capability, listener, CORS rule, or secret scope was widened
- [ ] T054 Complete the PR description from evidence in `specs/022-separate-control-data-fabrics/quickstart.md`, citing feature 022, ADR-0006, Principles I/III/IV/VI/VII, compatibility, rollback, and real-cluster measurements

---

## Dependencies and execution order

- Phase 1 has no dependencies.
- Phase 2 depends on Phase 1 and blocks every user story.
- US1 depends on Phase 2 and is the MVP.
- US2 depends on the endpoint/client foundation in Phase 2; its listener integration uses US1's split
  listener scaffold, so merge order is US1 then US2 even though most US2 tests can be written earlier.
- US3 depends on US1 and US2 because preflight and rollback validate both completed paths.
- Phase 6 depends on all selected stories and cannot waive the real-cluster gate.

## Parallel opportunities

- T002–T004 can run in parallel.
- T005, T006, T008, and T011 touch separate files and can run in parallel before their implementations.
- T014–T017, T028–T031, and T038–T039 are parallel test-writing groups.
- Dashboard/alert work T044–T045 can proceed alongside runbook work T046 after telemetry names stabilize.
- T049–T050 can run in parallel during polish.

## Implementation strategy

Deliver US1 first and stop to prove core-independent Studio control before changing replication.
Deliver US2 next while the old physical cable remains recoverable. Only then execute US3 preflight and
physical cutover. Retain the additive schema and legacy contract support through rollback; removal is
a future explicitly specified change.
