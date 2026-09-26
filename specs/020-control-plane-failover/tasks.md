# Tasks: Control-Plane Failover and Frontend Election

**Input**: Design documents from `specs/020-control-plane-failover/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`,
`quickstart.md`

**Tests**: Contract, unit, composed integration, topology/image-policy, and real-cluster fault
tests are required by the specification and Constitution v1.0.0.

## Phase 1: Setup

- [X] T001 Create the isolated `apps/coire-failover/` distribution, exact dependency evidence, and hardened image skeleton in `apps/coire-failover/pyproject.toml`, `apps/coire-failover/Dockerfile`, and `specs/020-control-plane-failover/review.md`
- [X] T002 [P] Define failover configuration and Keychain-secret documentation in `packages/coire-core/src/coire_core/settings.py` and `deploy/compose/README.md`
- [X] T003 [P] Add separate core, edge-a, and edge-b tunnel/LB configuration templates in `deploy/cloudflared/` and `deploy/compose/README.md`

## Phase 2: Foundational

- [X] T004 Add strict election, snapshot, override, journal, tier, and degraded response models in `packages/coire-core/src/coire_core/models/failover.py` and `packages/coire-core/src/coire_core/models/__init__.py`
- [X] T005 [P] Add signature, lease, expiry, stale-snapshot, and lone-survivor validation tests in `packages/coire-core/tests/test_failover_models.py`
- [X] T006 Implement core-signed atomic snapshot publication and idempotent event reconciliation in `apps/coire-api/src/coire_api/failover/snapshot.py` and `apps/coire-api/src/coire_api/failover/reconcile.py`
- [X] T007 Implement signed peer authentication, bounded election journal, and scoped relay credential loading in `apps/coire-node/src/coire_node/failover/journal.py` and `apps/coire-node/src/coire_node/failover/auth.py`
- [X] T008 [P] Add snapshot publisher, peer-auth, and journal restart tests in `apps/coire-api/tests/unit/test_failover_snapshot.py` and `apps/coire-node/tests/unit/test_failover_auth.py`
- [X] T009 Add the failover schema migration and reversible migration coverage in `apps/coire-api/alembic/versions/0013_failover_event_receipts.py` and `apps/coire-api/tests/unit/test_migrations.py`

**Checkpoint**: Every failover wire shape, signature boundary, and non-authoritative storage rule is tested.

## Phase 3: User Story 1 — Inference survives core loss (P1)

**Goal**: A quorum-elected Studio serves authenticated requests only for resident models.

**Independent Test**: Kill core, obtain edge-a lease, and complete an Access-authenticated completion
without any database or control-plane write.

- [X] T010 [P] [US1] Add route-absence, resident-only, stale-auth, streaming, and no-write contract tests in `apps/coire-failover/tests/contract/test_failover_gateway.py`
- [X] T011 [P] [US1] Add core-loss composed inference scenario in `tests/integration/test_control_plane_failover.py`
- [X] T012 [US1] Implement snapshot-backed Access authentication and tier response headers in `apps/coire-failover/src/coire_failover/auth.py` and `apps/coire-failover/src/coire_failover/tier.py`
- [X] T013 [US1] Implement resident-only model resolution and scoped local/peer inference proxy in `apps/coire-failover/src/coire_failover/resolution.py` and `apps/coire-node/src/coire_node/failover/relay.py`
- [X] T014 [US1] Implement only `/ready`, `/failover/tier`, `/v1/models`, and `/v1/chat/completions` in `apps/coire-failover/src/coire_failover/app.py`
- [X] T015 [US1] Complete the degraded chat UI in `apps/coire-web/src/pages/Failover.tsx`: sending a message must stream an authenticated completion, and the Studio image must serve the page and its assets.

## Phase 4: User Story 2 — Priority and safe election (P1)

**Goal**: Only the priority-eligible candidate with quorum can serve; a lone Studio fails closed.

**Independent Test**: Exercise core loss, competing candidates, edge-a loss, and lone edge-b;
verify exactly one lease or an explicit outage/break-glass state.

- [X] T016 [P] [US2] Add vote replay, competing term, priority delay, quorum-loss, and hand-back unit tests in `apps/coire-api/tests/unit/test_failover_election.py` and `apps/coire-node/tests/unit/test_studio_failover_election.py`
- [X] T017 [P] [US2] Add minority partition and lone-survivor composed tests in `tests/integration/test_control_plane_failover.py`
- [X] T018 [US2] Implement core participant self-fencing and signed membership/vote handling in `apps/coire-api/src/coire_api/failover/participant.py`
- [X] T019 [US2] Wire the Studio participant to a real standing memory-ledger reservation, frontend lifecycle, drain, and readiness fencing in `apps/coire-node/src/coire_node/failover/participant.py`.
- [X] T020 [US2] Deliver expiring audited inhibit and break-glass overrides to the Studio pollers, including a path usable when core is unavailable.

## Phase 5: User Story 3 — Split-brain prevention (P1)

**Goal**: A partitioned or stale member cannot receive ingress or serve new requests.

**Independent Test**: Induce every two-way partition and confirm readiness is successful only for the
current proof holder.

- [X] T021 [P] [US3] Add election-gated readiness and stale-proof contract tests in `apps/coire-failover/tests/contract/test_failover_ready.py`
- [X] T022 [P] [US3] Add control-VLAN and data-fabric partition scenarios in `tests/integration/test_control_plane_failover.py`
- [X] T023 [US3] Add signed peer-election endpoints restricted to existing listeners in `apps/coire-node/src/coire_node/routes/failover.py` and `apps/coire-api/src/coire_api/routes/failover.py`
- [X] T024 [US3] Add fail-closed Cloudflare LB monitors, pool policy, and Access route tests in `deploy/cloudflared/`, `deploy/compose/`, and `tests/integration/test_topology.py`

## Phase 6: User Story 4 — Explicit capability tiers (P2)

**Goal**: Degraded users see exactly what is available and unavailable.

**Independent Test**: Verify full, degraded-inference, and minimal tiers refuse all excluded actions
with a stable reason.

- [X] T025 [P] [US4] Add tier projection and mutation-route absence tests in `apps/coire-failover/tests/contract/test_failover_tier.py`
- [X] T026 [US4] Implement tier capability projection and stable refusal responses in `apps/coire-failover/src/coire_failover/tier.py`
- [X] T027 [US4] Add tier page, model availability, and unavailable-capability tests in `apps/coire-web/src/pages/Failover.tsx` and `apps/coire-web/src/pages/Failover.test.tsx`

## Phase 7: User Story 5 — Clean core hand-back (P2)

**Goal**: Core resumes only after an elected Studio drains and fences itself.

**Independent Test**: Restore a flapping core during an in-flight completion and verify drain,
single hand-back, event reconciliation, and no oscillation.

- [X] T028 [P] [US5] Add hand-back drain, recovery damping, and event-reconciliation tests in `apps/coire-api/tests/unit/test_failover_handback.py` and `apps/coire-node/tests/unit/test_studio_failover_election.py`
- [X] T029 [P] [US5] Add recovery/flapping composed scenarios in `tests/integration/test_control_plane_failover.py`
- [X] T030 [US5] Wire signed hand-back request/ack, real in-flight request accounting, drain deadline, and frontend stop into the running services; retain the standing ledger reservation per FR-015 and the clarified FR-018.
- [X] T031 [US5] Send bounded Studio event journals to core after recovery and reconcile them into audited core events.

## Phase 8: Polish and release gates

- [X] T032 Harden, compose, healthcheck, image-policy, CVE, and SBOM gates for `coire-failover` in `deploy/compose/compose.yaml`, `apps/coire-failover/Dockerfile`, and CI workflows
- [X] T033 [P] Add election/snapshot/ingress spans, metrics, dashboard panels, and alert rules in `apps/coire-api/`, `apps/coire-node/`, `apps/coire-failover/`, and `deploy/observability/`
- [X] T034 [P] Add activation, break-glass, observe, partition, rollback, and hand-back procedures in `docs/runbooks/control-plane-failover.md`
- [X] T035 Regenerate OpenAPI and TypeScript schemas; add freshness tests in `apps/coire-api/openapi.json`, `apps/coire-web/src/api/schema.d.ts`, and contract tests
- [ ] T036 Run unit, contract, web, compose, image, and migration gates; perform and record the mandatory real-cluster quickstart in `specs/020-control-plane-failover/review.md`
- [X] T037 Publish fresh, core-signed snapshots from the authoritative registry and identity settings; replicate them atomically to both Studios and start pollers when a valid snapshot arrives after process startup.
- [X] T038 Pin the trusted core snapshot verification key outside the snapshot and configure Keychain-sourced election keys on core and both Studios without using environment secrets.
- [X] T039 Complete Studio deployment and ingress: serve the degraded web assets from the failover image, configure both Studio relay endpoints, and prove the tunnel/LB templates resolve to the actual service listeners.
- [X] T040 Add composed tests that exercise the deployed pollers, snapshot delivery, override delivery, journal replay, frontend chat, and hand-back through running service boundaries.

## Dependencies and execution order

Setup and Foundational work block all stories. US1 requires T004–T009. US2 and US3 build on US1's
service boundary; US4 builds on US1; US5 depends on US2. Polish follows all stories.

Parallel opportunities: T002/T003, T005/T008, the paired tests in every story, and T033/T034.

## Implementation strategy

Deliver US1 first with a single safe edge-a quorum scenario. Add election fencing and ingress next;
then degraded UI and clean hand-back. Do not expose public Studio ingress until the contract,
topology, and real-cluster fault tests are green.
