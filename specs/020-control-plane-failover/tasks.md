# Tasks: Control-Plane Failover and Frontend Election

**Input**: Design documents from `specs/020-control-plane-failover/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`,
`quickstart.md`

**Tests**: Contract, unit, composed integration, topology/image-policy, and real-cluster fault
tests are required by the specification and Constitution v2.0.0.

## Execution status — 2026-10-14

**Scope amendment — 2026-10-14:** The owner explicitly directed that Cloudflare goes in last.
Implementation and authenticated local/Studio qualification continue now. Provider account
configuration and public-edge acceptance in T057/T073 are deferred to the Cloudflare rollout;
they are not passing results. Origin authentication, fencing, partitions, resident inference,
resource bounds and recovery remain required. No authentication or network exception is implied.

T001–T040 retain their historical status (T036 remains open). Corrective implementation
T041–T055, T059–T064 and T066–T072 is implemented and validated as recorded in `review.md`.
T058 has independent process restart/partition/epoch admission coverage and isolated rank/link
fault coverage; physical link qualification remains in T073. T065 remains open for the complete
armed-020 upgrade-success/failed-candidate rollback matrix: full application maintenance fencing,
frontend-start admission, cohort recovery and SQL handover ownership checks pass, but these do
not establish the entire live operation. T056/T057 provider-dependent work is owner-deferred.
T073/T036 remain open: real TP/PP buffered/streaming and both rank faults pass with zero swap;
all four mid-stream interruption cases pass. Temporary en5 aliases were applied and removed by
the owner, with cleanup readback. Core auto-restart is confirmed; physical power loss is untested.
Participant resource windows failed the Studio CPU budget; the renewal correction is under retest. Deployed coexistence/public timing
trials are not claimed. All passing and failed attempts are recorded without counting skips as passes.

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
- [X] T020 [US2] Deliver expiring audited inhibit and break-glass overrides to the Studio pollers, including a path usable when core is unavailable in `apps/coire-api/src/coire_api/routes/admin_failover.py` and `apps/coire-node/src/coire_node/routes/failover.py`.

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
- [X] T030 [US5] Wire signed hand-back request/ack, real in-flight request accounting, drain deadline, and frontend stop into the running services; retain the standing ledger reservation per FR-015 and the clarified FR-018 in `apps/coire-node/src/coire_node/failover/participant.py` and `apps/coire-failover/src/coire_failover/app.py`.
- [X] T031 [US5] Send bounded Studio event journals to core after recovery and reconcile them into audited core events in `apps/coire-node/src/coire_node/failover/poller.py` and `apps/coire-api/src/coire_api/failover/reconcile.py`.

## Phase 8: Polish and release gates

- [X] T032 Harden, compose, healthcheck, image-policy, CVE, and SBOM gates for `coire-failover` in `deploy/compose/compose.yaml`, `apps/coire-failover/Dockerfile`, and CI workflows
- [X] T033 [P] Add election/snapshot/ingress spans, metrics, dashboard panels, and alert rules in `apps/coire-api/`, `apps/coire-node/`, `apps/coire-failover/`, and `deploy/observability/`
- [X] T034 [P] Add activation, break-glass, observe, partition, rollback, and hand-back procedures in `docs/runbooks/control-plane-failover.md`
- [X] T035 Regenerate OpenAPI and TypeScript schemas; add freshness tests in `apps/coire-api/openapi.json`, `apps/coire-web/src/api/schema.d.ts`, and contract tests
- [ ] T036 Close the aggregate release gate only after T041–T073: reconcile all required passing evidence in `specs/020-control-plane-failover/review.md`, mark qualified tasks truthfully, and update `docs/ROADMAP.md` plus the PR template with Principles I–VII compliance.
- [X] T037 Publish fresh, core-signed snapshots from the authoritative registry and identity settings; replicate them atomically to both Studios and start pollers when a valid snapshot arrives after process startup in `apps/coire-api/src/coire_api/failover/publication.py` and `apps/coire-node/src/coire_node/failover/poller.py`.
- [X] T038 Pin the trusted core snapshot verification key outside the snapshot and configure Keychain-sourced election keys on core and both Studios without using environment secrets in `packages/coire-core/src/coire_core/settings.py` and `deploy/compose/README.md`.
- [X] T039 Complete Studio deployment and ingress: serve the degraded web assets from the failover image, configure both Studio relay endpoints, and prove the tunnel/LB templates resolve to the actual service listeners in `deploy/cloudflared/`, `deploy/compose/compose.yaml`, and `apps/coire-failover/Dockerfile`.
- [X] T040 Add composed tests that exercise the deployed pollers, snapshot delivery, override delivery, journal replay, frontend chat, and hand-back through running service boundaries in `tests/integration/test_control_plane_failover.py`.

## Phase 9: Corrective setup and foundations

**Goal**: Establish an honest baseline, strict authority contracts and restart-safe election before
changing serving behavior. Covers FR-001–011, FR-025, FR-027, FR-030 and SC-002/003/012/017.

- [X] T041 Record the current route/credential, deployment, acceptance-prerequisite and historical-test inventory in `specs/020-control-plane-failover/review.md`; identify exact external blockers without changing live configuration.
- [X] T042 Add strict model rejection tests for version/epoch, lifetime, exactly-one authority, host-bound overrides, relay context, acknowledgements, limits and fresh heartbeat challenges in `packages/coire-core/tests/test_failover_models.py`.
- [X] T043 Implement the contracts and validated settings from `specs/020-control-plane-failover/data-model.md` in `packages/coire-core/src/coire_core/models/failover.py` and `packages/coire-core/src/coire_core/settings.py`; include explicit compatible rollout rules and typed refusal/acknowledgement models.
- [X] T044 Add independent restart/overlapping-term/self-grant/clock-jump/lowered-TTL/membership/replay regressions in `packages/coire-core/tests/test_failover_poller.py` and `apps/coire-node/tests/unit/test_studio_failover_election.py`; assert no conflicting request admission.
- [X] T045 Implement durable outstanding-grant and trusted-epoch guards, conservative restart quarantine, monotonic deadlines with bounded clock uncertainty, and challenge-bound heartbeat freshness in `packages/coire-core/src/coire_core/failover_election.py`, `packages/coire-core/src/coire_core/failover_poller.py`, and both service participant modules; resolve declared control addresses in poller/subscriber/override delivery instead of hard-coded names.

## Phase 10: US1 — Authenticated, bounded resident inference (P1)

**Independent test**: Fresh authority and supported resident target complete a request; stale/wrong
identity, holder, target, maintenance state or limit produces no engine work. FR-008–013, FR-015/016,
FR-023–026, FR-030; SC-001/004/005/009/013/017.

- [X] T046 [P] [US1] Add every-route/asset credential-scope tests for browser, monitor, local lifecycle and peer identities in `apps/coire-failover/tests/contract/test_failover_ready.py`, `apps/coire-failover/tests/contract/test_failover_gateway.py`, and `apps/coire-node/tests/contract/test_node_failover.py`; include anonymous direct-origin requests.
- [X] T047 [US1] Enforce the declared route identities and typed refusals in `apps/coire-failover/src/coire_failover/app.py`, `apps/coire-failover/src/coire_failover/auth.py`, `apps/coire-api/src/coire_api/routes/failover.py`, and the authenticated in-flight query in `apps/coire-node/src/coire_node/failover/poller.py`.
- [X] T048 [US1] Add full-API and node relay admission tests for stale routing, wrong holder/epoch/snapshot, static-token-only calls, and expiry during residency I/O in `tests/integration/test_control_plane_failover.py` and `apps/coire-node/tests/contract/test_node_failover.py`.
- [X] T049 [US1] Fence actual Core gateway and Studio frontend admission, issue request-bound relay authority, and independently validate it on the node in `apps/coire-api/src/coire_api/app.py`, `apps/coire-api/src/coire_api/gateway/`, `apps/coire-failover/src/coire_failover/app.py`, `apps/coire-failover/src/coire_failover/proxy.py`, and `apps/coire-node/src/coire_node/routes/failover.py`; recheck immediately before engine work.
- [X] T050 [US1] Add FR-013 supported-target/capability, nonresident, exact-target, visual/tool, unknown/ineligible identity, mutable-budget dependency, prompt/output, rate and concurrency contract tests in `apps/coire-failover/tests/contract/test_failover_gateway.py` and `apps/coire-node/tests/contract/test_node_failover.py`; prove refusal creates no engine lifecycle action or billing row.
- [X] T051 [US1] Enforce the confirmed FR-013 scope using signed capability/placement/limit metadata and exact live residency in `apps/coire-api/src/coire_api/failover/snapshot.py`, `apps/coire-failover/src/coire_failover/resolution.py`, `apps/coire-failover/src/coire_failover/app.py`, and `apps/coire-node/src/coire_node/routes/failover.py`; use bounded conservative local counters without persisting conversations or billing.

- [X] T052 [US1] Add exact-group/rank/link proof, stale/ambiguous placement, rank loss during a stream, and minimal-tier sharding refusal tests in `apps/coire-failover/tests/contract/test_failover_gateway.py`, `apps/coire-node/tests/contract/test_node_failover.py`, and `tests/integration/test_control_plane_failover.py` for FR-031/SC-018; assert no fallback or lifecycle action.
- [X] T053 [US1] Implement resident sharded inference with signed instance/group-generation/rank metadata and live authenticated all-rank/link checks in `packages/coire-core/src/coire_core/models/failover.py`, `apps/coire-api/src/coire_api/failover/snapshot.py`, `apps/coire-node/src/coire_node/sharding.py`, `apps/coire-node/src/coire_node/routes/failover.py`, and `apps/coire-failover/src/coire_failover/resolution.py`; extend proxy lifetime checks to interrupt on rank/link loss within 3 s and never re-place or substitute a target.

## Phase 11: US2 — Priority and one-host break-glass (P1)

**Independent test**: Lone survivor refuses automatic promotion; a fenced, fresh, targeted override
works on exactly one host and expires. FR-003–007, FR-020/021/025/027; SC-002/003/011/012/016.

- [X] T054 [P] [US2] Add wrong-host/epoch/kind, duplicate/replayed override, actor mismatch, audit failure and missing independent-fence evidence tests in `apps/coire-api/tests/unit/test_failover_overrides.py` and `apps/coire-node/tests/unit/test_studio_failover_election.py`.
- [X] T055 [US2] Implement single-target delivery, human actor binding, durable replay protection, audit-intent-before-delivery and bounded offline activation in `apps/coire-api/src/coire_api/routes/admin_failover.py`, `apps/coire-api/src/coire_api/failover/overrides.py`, `apps/coire-node/src/coire_node/failover/participant.py`, and `apps/coire-node/src/coire_node/routes/failover.py`; never extend snapshot validity.

## Phase 12: US3 — Partition-safe ingress (P1)

**Independent test**: All induced partitions, stale routes and all-unhealthy monitor states admit
through at most one authority. FR-004–007/019/026/027; SC-002/003/012/016.

- [ ] T056 [P] [US3] Replace placeholder-only ingress assertions with supported-policy, monitor-scope, fallback and stale-origin refusal tests in `tests/integration/test_topology.py` and `tests/integration/test_control_plane_failover.py`; include control/data listener isolation.
- [ ] T057 [US3] Reconcile `deploy/cloudflared/load-balancer.yaml.tmpl`, tunnel templates, `deploy/cloudflared/README.md`, and `deploy/compose/compose.yaml` with authenticated no-cache readiness, ordered provider-supported pools and disabled non-serving fallback; keep secrets outside Git and require separate approval for any necessary network/Access expansion.
- [X] T058 [US3] Add process-boundary participant restart, every majority/minority partition, stale Core route, epoch rotation and data-link-loss cases in `tests/integration/test_control_plane_failover.py`; independently observe proof lifetime and actual request admission rather than only participant roles.

## Phase 13: US4 — Truthful tier and outage UI (P2)

**Independent test**: Browser displays no-history and expiry limits before sending, distinguishes a
lone fenced outage from working minimal service, and refuses unsupported capabilities clearly.
FR-012/014/023/026; SC-005/006/013/016.

- [X] T059 [P] [US4] Add expiry/outage/capability/unsent-draft/accessibility tests in `apps/coire-failover/tests/contract/test_failover_tier.py`, `apps/coire-web/src/api/failover.test.ts`, and `apps/coire-web/src/pages/Failover.test.tsx`.
- [X] T060 [US4] Update `apps/coire-failover/src/coire_failover/tier.py`, `apps/coire-web/src/api/failover.ts`, and `apps/coire-web/src/pages/Failover.tsx` to distinguish admission availability, signed-authority expiry, supported scope and stable refusal reasons using generated schemas; preserve drafts without claiming persistence.

## Phase 14: US5 — Bounded recovery, audit and upgrade coexistence (P2)

**Independent test**: In-flight requests finish only while authorised and within deadline; recovery
produces one hand-back, retained reservations, complete audit and unchanged 019 writer ownership.
FR-006/007/015–018/021/027/028; SC-005/007–010/012/015/016.

- [X] T061 [P] [US5] Add streaming and buffered expiry/cancellation, authenticated accounting, deadline and delayed fence-ack tests in `apps/coire-failover/tests/unit/test_proxy.py`, `apps/coire-api/tests/unit/test_failover_handback.py`, and `apps/coire-node/tests/unit/test_studio_failover_election.py`.
- [X] T062 [US5] Preserve bounded drain authority, monitor lease/snapshot/JWT expiry throughout requests, cancel upstream work and require fence acknowledgement or conservative expiry before successor admission in `packages/coire-core/src/coire_core/failover_election.py`, `apps/coire-failover/src/coire_failover/app.py`, `apps/coire-failover/src/coire_failover/proxy.py`, `apps/coire-node/src/coire_node/failover/relay.py`, and `apps/coire-node/src/coire_node/failover/poller.py`.
- [X] T063 [P] [US5] Add full/corrupt/unwritable journal, reserved terminal-event, restart recovery and concurrent duplicate acknowledgement tests in `apps/coire-node/tests/unit/test_failover_auth.py` and `apps/coire-api/tests/unit/test_failover_reconcile.py`.
- [X] T064 [US5] Implement non-lossy bounded journals, acknowledged compaction, audit-before-admission transition ordering and idempotent concurrent receipt reconciliation in `apps/coire-node/src/coire_node/failover/journal.py`, `apps/coire-node/src/coire_node/failover/poller.py`, `apps/coire-api/src/coire_api/failover/reconcile.py`, and `apps/coire-api/src/coire_api/routes/failover.py`; include Core event retention and actor/reason evidence. Add one reversible new migration only if receipt storage requires it; preserve migration 0013 and existing audit rows.
- [ ] T065 [US5] Add full-middleware Core upgrade success/failed-candidate rollback and Studio maintenance-acquisition races with frontend start/streaming relay in `tests/integration/test_control_plane_failover.py` and `tests/integration/test_upgrade_handover_authority.py`; assert exact owner/data preservation and no fabricated heartbeat failure.
- [X] T066 [US5] Integrate authenticated safety exchanges, authoritative-cohort hand-back, node relay/start admission and active-stream inventory with existing 019 guards in `apps/coire-api/src/coire_api/upgrades/admission.py`, `apps/coire-api/src/coire_api/upgrades/inventory.py`, `apps/coire-api/src/coire_api/failover/participant.py`, `apps/coire-node/src/coire_node/failover/poller.py`, and `apps/coire-node/src/coire_node/routes/failover.py`; never transfer writer ownership or bypass guarded database writes.

## Phase 15: Qualification and handoff

**Goal**: Prove the whole corrected feature, including mandatory real-cluster acceptance. FR-001–031;
SC-001–018. Required checks must pass; historical skips are not release evidence.

- [X] T067 [P] Add actual failover metric-export/collector-loss tests in `tests/integration/test_observability_health.py` and absent-participant/audit-pressure/resource/deadline alert scenarios in `tests/observability/failover.alert.test.yaml` plus `tests/test_alert_rules.py` for lean and diagnostic profiles.
- [X] T068 Initialize and verify bounded telemetry through the approved Studio control path in `apps/coire-failover/src/coire_failover/__main__.py` and `deploy/compose/compose.yaml`; extend `packages/coire-core/src/coire_core/failover_telemetry.py`, `deploy/observability/alerts/failover.yaml`, and `deploy/observability/grafana/dashboards/failover.json` without adding a Studio backend or retrying disabled stores.
- [X] T069 [P] Update `docs/runbooks/control-plane-failover.md` and `deploy/compose/README.md` with scoped secrets/settings, snapshot horizon, actor-bound offline override/fencing, resource measurement, sharded rank/link refusal, audit repair, bounded drain, versioned disarm/update/re-arm, 019-compatible rollback and kill procedures; include actual supported monitor setup.
- [X] T070 Regenerate `apps/coire-api/openapi.json`, `apps/coire-failover/openapi.json`, `apps/coire-web/src/api/schema.d.ts`, and `apps/coire-web/src/api/failover-schema.d.ts`; assert generated node schemas in `apps/coire-node/tests/contract/test_node_failover.py` and run existing freshness checks with every schema change.
- [X] T071 Add/run an isolated real tiny-model failover smoke in `tests/integration/test_failover_engine.py` and the completed process-boundary fault suite in `tests/integration/test_control_plane_failover.py`; register required isolated cases in `.github/workflows/ci.yml`, never target real Studios, and record exact results in `specs/020-control-plane-failover/review.md`.
- [X] T072 Run required Python/web/type/schema/migration/alert and production image build/policy/scan/SBOM gates for the integrated change; record exact commit/digests and results in `specs/020-control-plane-failover/review.md`; rerun only affected or failed areas after fixes.
- [ ] T073 Execute every mandatory live case in `specs/020-control-plane-failover/quickstart.md` and record public ingress, power-loss, partitions, bounded break-glass, 60/90-second timing, 15-minute overhead, actual swap, coexistence and rollback evidence in `specs/020-control-plane-failover/review.md`; keep blocked or failed cases open, then return to T036.

## Dependencies and execution order

Story phases group related work; they are not a requirement to finish an unsafe partial activation
before its later safety dependency. A valid implementation order is **T041–T051 → T061–T064 →
T052–T058 → T059–T060 → T065–T073 → T036**. This delivers the stream and audit interfaces before
sharded streaming and break-glass activation. Generate schemas incrementally whenever contracts
change; T070 is also the final freshness checkpoint.

- T041 → T042 → T043 → T044 → T045 establishes the corrective foundation. Red/green regression
  development is allowed; do not merge failing checkpoints.
- US1: T046 → T047 → T048 → T049 → T050 → T051; then T052 → T053 after T062. T049 needs T045; schema edits always precede
  service/web consumers. T070 regeneration runs incrementally with schema changes, not only at end.
- US2: T054 → T055 after T045, T047 and T064. T064 supplies audit capacity/ordering before
  any override activation or its end-to-end acceptance.
- US3: T056 → T057 → T058 after T045/T049/T053/T055. T058 covers startup and replay across real process
  boundaries; clock/corruption fault injection stays isolated.
- US4: T059 → T060 after T043/T047/T051 and the corresponding T070 generated types.
- US5: T061 → T062, T063 → T064, T065 → T066. T062 requires admission authority from T049;
  T064 depends on T043/T047 and provides the activation/audit gate for T055; T066 requires T062/T064 and uses existing
  019 ownership without modifying its handover semantics.
- T067 → T068 after the instrumented service changes. T069 follows final operational behavior.
  T070 final freshness → T071 isolated qualification → T072 release checks → T073 live acceptance
  → T036 aggregate closure. External account/credential/approval blockers do not stop independent
  implementation, and no missing live case can be counted as qualified.

## Parallel opportunities by story

These are dependency opportunities, not an instruction to start implementation during this pass.
After foundations, US1 route tests T046 and US2 override tests T054 can be authored separately.
US3 policy tests T056 can proceed while US1 service work is underway; integration waits for its
stated dependencies. US4 web/API-client tests in T059 can proceed after schema decisions. US5
stream tests T061 and journal tests T063 touch distinct files and can proceed separately.
Observability fixtures T067 and runbook drafting T069 can proceed after their interfaces settle.
Shared source files, schema regeneration, live mutations and acceptance runs remain sequential.

## Implementation strategy and MVP

The smallest demonstrable increment is US1 with one safe edge-a quorum and resident request after
foundations; this is an isolated milestone, not a release. The public release requires all authority,
break-glass, partition, recovery, observability and real-cluster gates. Reuse the existing service,
record targeted evidence, and keep the acceptance configuration fixed rather than expanding scope
or relaxing limits to obtain a pass.
