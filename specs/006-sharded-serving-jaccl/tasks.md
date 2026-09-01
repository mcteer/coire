# Tasks: Sharded Serving over JACCL

**Input**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

## Phase 1: Contracts and persistence

- [x] T001 Add strict link observation/verdict, shard group/rank command and benchmark contracts in `packages/coire-core/src/coire_core/models/sharding.py`
- [x] T002 Export sharding contracts and extend placement/instance contracts without caller host/path/argv fields
- [x] T003 [P] Add coire-core contract validation and malicious-input tests
- [x] T004 Add link observation, shard group and benchmark ORM rows plus member rank-health fields
- [x] T005 Create reversible `0008_sharded_serving.py` migration and migration-chain tests
- [x] T006 Add link damping, probe freshness and fallback settings with documented defaults (no latency gate)

## Phase 2: Link evidence and atomic admission

- [x] T007 Implement append-only link observations and 2-failure/3-success projection service
- [x] T008 Implement authenticated admin probe/state routes and audit mutations
- [x] T009 Add node probe contracts/routes for explicit JACCL all-reduce and ring fallback argv
- [x] T010 Implement generated hostfile validation from declared Studio data endpoints; reject core
- [x] T011 Implement two-node sorted advisory locking and all-or-none reservation/eviction
- [x] T012 [P] Test damping, high-latency non-refusal, probe absence/failure gates and flapping
- [x] T013 [P] Test competing admissions never split reservations or deadlock

## Phase 3: User Story 1 — two-rank serving (P1)

- [x] T014 [US1] Add persisted coire-node shard-group manager with explicit `mlx.launch`/`mlx_lm.server` argv
- [x] T015 [US1] Add authenticated prepare/start/status/stop group routes and idempotent command identity
- [x] T016 [US1] Extend instance launch workflow for TP/PP, two members and coordinated warming
- [x] T017 [US1] Route gateway traffic only to ready rank 0 while leases cover both reservations
- [x] T018 [US1] Extend drain/TTL/LRU teardown to confirm both ranks before atomic release
- [x] T019 [P] [US1] Add argv, hostfile, adoption, partial-launch and teardown unit/contract tests
- [x] T020 [P] [US1] Add composed fake two-rank create/stream/state/drain scenario

## Phase 4: User Story 2 — rank failure and fallback (P1)

- [x] T021 [US2] Reconcile rank conjunction; fail instance and degrade failed node on any rank loss
- [x] T022 [US2] Issue idempotent whole-group teardown and release both reservations together
- [x] T023 [US2] Translate rank loss to prompt gateway failure and `Retry-After`
- [x] T024 [US2] Implement one bounded durable smaller-variant fallback on the healthy survivor
- [x] T025 [US2] Persist no-fit terminal condition without retry thrash
- [x] T026 [P] [US2] Add composed mid-stream rank kill, teardown, degraded node and fallback/no-fit tests

## Phase 5: User Story 3 — measured link (P2)

- [x] T027 [US3] Trigger probe on first eligible boot and OS/engine version change
- [x] T028 [US3] Gate TP on current successful RDMA evidence; retain PP and single-node paths
- [x] T029 [US3] Surface raw measurements, projection, figures and flapping in cluster state
- [x] T030 [P] [US3] Add composed link-down, unmeasured, high-latency and recovery tests

## Phase 6: User Story 4 — placement benchmark (P2)

- [x] T031 [US4] Implement authenticated/audited benchmark create/list routes
- [x] T032 [US4] Run single-A, TP and PP sequentially and record tokens/s plus conditions/GPU cores
- [x] T033 [US4] Preserve repeated results append-only and expose typed model comparison
- [x] T034 [P] [US4] Add benchmark contract/unit/composed tests

## Phase 7: Observability, docs and gates

- [x] T035 Add `coire.sharding.*` spans, rank/group/link/benchmark metrics and structured fields
- [x] T036 Add dashboard panels and rank-down, stale-probe, no-fit and flapping alerts
- [x] T037 Update OpenAPI/generated TypeScript and document configuration
- [x] T038 Add `docs/runbooks/sharded-serving.md` including see/kill/rollback and beta warning
- [x] T039 Run Ruff, strict mypy, unit/contract, web and OpenAPI gates
- [x] T040 Run full composed suite including fake ranks, failures, fallback and restart recovery
- [x] T041 Build changed images and pass policy plus CRITICAL scans
- [ ] T042 Execute `quickstart.md` on the real two-Studio JACCL fabric and record evidence
- [ ] T043 Complete PR description with spec and Principles I–VII compliance

## Dependencies

T001→T004→T005. T007/T009/T010→T011. US1 requires T011; US2 requires US1; US3 builds on
link evidence and admission; US4 requires stable single/TP/PP lifecycle. T035–T043 follow stories.

## Phase 8: Convergence

- [x] T044 Implement coordinated two-ledger LRU eviction before atomic sharded reservation per FR-003/T011 (partial)
- [x] T045 Encode retry guidance in terminal SSE failure and test prompt rank-loss termination per FR-012/SC-002/T023 (partial)
- [x] T046 Trigger link probes on first eligible boot and OS/engine-version changes per FR-007/T027 (partial)
- [x] T047 Add composed partial-launch, restart-recovery, unmeasured/down/high-latency/recovery coverage per T019/T026/T030/T040 (partial)
- [x] T048 Complete `coire.sharding.*` spans and structured group/rank/link/benchmark transition logs per Constitution VI/T035 (partial)

## Phase 9: Convergence

- [x] T049 Add a synthetic >250 GB two-ledger composed admission/gateway scenario and retain the real-model live gate per SC-001 (partial)
- [x] T050 Prove a single-node gateway request remains successful while RDMA evidence is down per SC-007 (partial)
- [x] T051 Amend the streaming failure contract so pre-header rank loss returns HTTP 503/`Retry-After` and post-header loss emits a terminal SSE retry directive per FR-012/SC-002 (contradicts)
- [x] T052 Make `quickstart.md` hostfile generation commands explicit for both JACCL and ring outputs per FR-002 (partial)
