# Tasks: Observability Stack

**Input**: `specs/009-observability-stack/` design artifacts

## Phase 1: Setup

- [x] T001 Record official image versions, digests, licences, and resource ceilings in `specs/009-observability-stack/research.md`
- [x] T002 [P] Create observability configuration directories under `deploy/compose/{prometheus,alertmanager,loki,tempo,grafana/provisioning}`
- [x] T003 [P] Add configurable retention, health-threshold, sampling-budget, and local endpoint settings in `apps/coire-api/src/coire_api/config.py` and `apps/coire-node/src/coire_node/config.py`

## Phase 2: Foundational

- [x] T004 Add strict node observation, process observation, node verdict, and cluster-health contracts in `packages/coire-core/src/coire_core/models/health.py`
- [x] T005 [P] Add contract model validation tests in `packages/coire-core/tests/test_models.py`
- [x] T006 Add reversible node health observation/counter persistence in `apps/coire-api/src/coire_api/db.py` and `apps/coire-api/alembic/versions/0008_observability_health.py`
- [x] T007 Add fail-open shared telemetry setup, W3C propagation, bounded attributes, and secret/content redaction in `apps/coire-api/src/coire_api/otel.py`
- [x] T008 [P] Extend native fail-open telemetry and structured logging setup in `apps/coire-node/src/coire_node/otel.py`
- [x] T009 [P] Add telemetry safety/cardinality/redaction tests in `apps/coire-api/tests/unit/test_otel.py` and `apps/coire-node/tests/unit/test_otel.py`

## Phase 3: User Story 1 — Slow request attribution (P1)

**Independent Test**: Delay each request stage and verify it is the dominant span in one propagated trace.

- [x] T010 [P] [US1] Add span-tree and W3C propagation tests in `tests/integration/test_observability_traces.py`
- [x] T011 [P] [US1] Instrument gateway handling and correlation fields in `apps/coire-api/src/coire_api/gateway/service.py`
- [x] T012 [US1] Instrument admission and cold-load waits in `apps/coire-api/src/coire_api/gateway/loading.py`
- [x] T013 [P] [US1] Instrument node load, prefill, decode, and sharded network boundaries in `apps/coire-node/src/coire_node/engines/manager.py` and `apps/coire-node/src/coire_node/sharding.py`
- [x] T014 [US1] Add bounded request/latency/token metrics with trace exemplars in `apps/coire-api/src/coire_api/otel.py`
- [x] T015 [US1] Validate collector-down failure isolation and six-stage attribution in `tests/integration/test_observability_traces.py`

## Phase 4: User Story 2 — Actionable alerts (P1)

**Independent Test**: Feed each induced condition and verify the named, grouped alert reaches firing state.

- [x] T016 [P] [US2] Add alert syntax, subject-label, grouping, and induced-expression tests in `tests/test_observability_alerts.py`
- [x] T017 [US2] Add recording and alert rules for unreachable/degraded/saturation/thermal/link/flapping/drift/load/run/tunnel/skew in `deploy/compose/prometheus/rules/coire-platform.yml`
- [x] T018 [US2] Configure subject grouping and inhibition in `deploy/compose/alertmanager/alertmanager.yml`
- [x] T019 [US2] Add dashboard and runbook annotations to every rule in `deploy/compose/prometheus/rules/coire-platform.yml`

## Phase 5: User Story 5 — End-to-end cluster health (P1)

**Independent Test**: Saturate a responding fixture and verify degraded; stop heartbeat and verify damped unreachable; expire it and verify unknown.

- [x] T020 [P] [US5] Add health state-machine, skew, freshness, and hysteresis unit tests in `apps/coire-api/tests/unit/test_health_evaluator.py`
- [x] T021 [P] [US5] Add aggregate health contract tests in `apps/coire-api/tests/contract/test_health_api.py`
- [x] T022 [US5] Implement health evaluation and asymmetric hysteresis in `apps/coire-api/src/coire_api/health_evaluator.py`
- [x] T023 [US5] Persist server receipt time, node sample time, live observation, and counters in `apps/coire-api/src/coire_api/nodes_prober.py`
- [x] T024 [US5] Expose truthful aggregate node/control/link health and heartbeat age in `apps/coire-api/src/coire_api/routes/health.py`
- [x] T025 [US5] Reject stale/nonhealthy observations in placement-compatible reads in `apps/coire-api/src/coire_api/registry/placement.py`
- [x] T026 [US5] Add no-thrash and stale-placement integration tests in `tests/integration/test_observability_health.py`

## Phase 6: User Story 4 — Node resource behavior (P2)

**Independent Test**: Load a fake engine and observe node/process resources, queue/throughput, lifecycle duration, thermal/GPU availability, and collection cost.

- [x] T027 [P] [US4] Add node and process sampling/budget/backoff tests in `apps/coire-node/tests/unit/test_metrics.py`
- [x] T028 [US4] Extend node sampling with used/reserved memory, bounded process CPU/RSS, duration, and collection-cost fields in `apps/coire-node/src/coire_node/metrics.py`
- [x] T029 [US4] Implement optional-sampler exponential backoff when over budget in `apps/coire-node/src/coire_node/metrics.py`
- [x] T030 [P] [US4] Record load/unload duration, queue depth, throughput, GPU/thermal availability, and clock skew metrics in `apps/coire-node/src/coire_node/metrics.py`
- [x] T031 [US4] Update node health route contract and tests in `apps/coire-node/src/coire_node/routes.py` and `apps/coire-node/tests/contract/test_node_health.py`

## Phase 7: User Story 3 — Standing dashboards (P2)

**Independent Test**: Load all three provisioned dashboards and follow every trace/log data link.

- [x] T032 [P] [US3] Configure Prometheus scrape/rule/Alertmanager routing in `deploy/compose/prometheus/prometheus.yml`
- [x] T033 [P] [US3] Configure seven-day local Loki TSDB retention and limits in `deploy/compose/loki/loki.yml`
- [x] T034 [P] [US3] Configure seven-day local Tempo storage/compaction in `deploy/compose/tempo/tempo.yml`
- [x] T035 [P] [US3] Configure collector memory limiting, batching, redaction, local exporters, and self-telemetry in `deploy/compose/otel-collector.yaml`
- [x] T036 [US3] Add hardened, constrained Prometheus/Alertmanager/Loki/Tempo/Grafana services and volumes in `deploy/compose/compose.yaml`
- [x] T037 [P] [US3] Provision local Prometheus/Loki/Tempo data sources and dashboard provider in `deploy/compose/grafana/provisioning/datasources/coire.yml` and `deploy/compose/grafana/provisioning/dashboards/coire.yml`
- [x] T038 [P] [US3] Build Cluster dashboard with aggregate/core/node/link resource truth and Explore links in `deploy/compose/grafana/provisioning/dashboards/cluster.json`
- [x] T039 [P] [US3] Build Traffic dashboard with rate/latency/tokens bounded breakdowns and trace/log links in `deploy/compose/grafana/provisioning/dashboards/traffic.json`
- [x] T040 [P] [US3] Build Jobs dashboard with durations/outcomes and trace links in `deploy/compose/grafana/provisioning/dashboards/jobs.json`
- [x] T041 [US3] Add deterministic config/dashboard schema and data-link tests in `tests/test_observability_config.py`
- [x] T042 [US3] Validate all dashboards at desktop/mobile widths using WebKit and record evidence in `specs/009-observability-stack/review.md`

## Phase 8: Polish and cross-cutting gates

- [x] T043 [P] Document configuration, retention, content policy, panel/rule convention, diagnosis, induced alerts, rollback, and real-cluster safety in `docs/runbooks/observability.md` and `deploy/compose/README.md`
- [x] T044 [P] Update observability architecture facts and explicit core host-telemetry boundary in `docs/ARCHITECTURE.md`
- [x] T045 Regenerate OpenAPI and TypeScript types in `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`
- [x] T046 Run ruff, strict mypy, unit/contract tests, web tests/lint/build, OpenAPI freshness, and compose validation
- [x] T047 Run clean-stack integration scenarios for trace propagation, collector outage, alert evaluation, dashboards, health damping, and stale placement
- [x] T048 Build every production image, enforce hardening, generate SPDX SBOMs, and scan for CRITICAL CVEs
- [x] T049 Measure Studio collection under inference load and record CPU/RSS/backoff evidence in `specs/009-observability-stack/review.md`
- [x] T050 Run convergence, complete appended work, re-run all gates, commit, push, and open a draft PR with constitution/licence notes

## Phase 9: Convergence

- [x] T051 Emit bounded node queue-depth, generation-throughput, and unload-duration metrics with unit and contract coverage per FR-006 (partial)
- [x] T052 Emit bounded gateway token metrics by identity class and model and prove the Traffic dashboard query is backed by a real instrument per FR-008 (missing)
- [x] T053 Add explicit interconnect state to the aggregate health contract and route with freshness-aware tests per FR-012a (partial)
- [x] T054 Add a local live producer for `coire_tunnel_up` and validate the tunnel alert against runtime input per FR-011 and SC-003 (missing)
- [x] T055 Add deterministic delayed-stage trace trials covering gateway, admission, load, prefill, decode, and network attribution per SC-001 (partial)
- [x] T056 Add live stale-health and flapping/no-placement-thrash integration coverage per SC-013 and SC-015 (missing)
- [x] T057 Assert configured seven-day backend retention fits the declared core disk allocation per SC-009 (partial)

## Dependencies and execution order

Setup → Foundational → US1/US2/US5 (P1) → US4/US3 (P2) → cross-cutting gates. US1, US2, and US5 are independently testable after the foundation. Backend configurations and dashboard JSON tasks marked `[P]` touch separate files.

## MVP

Foundation plus US1 proves the roadmap acceptance bar. Feature completion still requires every story and Phase 8.
