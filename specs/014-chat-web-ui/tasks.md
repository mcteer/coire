# Tasks: Chat Web UI

**Branch**: `feat/014-chat-web-ui`
**Inputs**: [spec](spec.md), [plan](plan.md), [research](research.md), [data model](data-model.md), [contracts](contracts/chat-api.md), [acceptance](quickstart.md)

Tests precede the corresponding implementation per Constitution VII; schema changes precede services/web per III. `[P]` indicates independent files once the phase prerequisites are complete, not permission to bypass dependencies.

## Phase 1 — Setup

- [X] T001 Record base revision, existing migration head, baseline gates and review-size delivery assessment in specs/014-chat-web-ui/review.md; follow CONTRIBUTING.md spec-splitting rules if the implementation exceeds a reviewable PR.
- [X] T002 Declare exact planned Markdown, worker PDFium/Pillow and Darwin node MLX-VLM dependencies in apps/coire-web/package.json, apps/coire-file-worker/pyproject.toml and apps/coire-node/pyproject.toml; synchronize apps/coire-web/package-lock.json, apps/coire-web/pnpm-lock.yaml and uv.lock, preserving licences/platform isolation.
- [X] T003 Reconcile the proposed architecture decision with implementation boundaries in docs/adr/0008-multimodal-chat-processing.md and docs/ARCHITECTURE.md, including bare VLM, CPU file worker, private volumes and text-only failover.

## Phase 2 — Foundation

**Goal:** Typed data, persistence, authorization and shared transport before any story.

- [X] T004 [P] Add canonical/native schema tests for strict content parts, ownership metadata, selections, turn/events, worker payloads and bounds in packages/coire-core/tests/test_chat_models.py and packages/coire-core/tests/test_file_models.py.
- [X] T005 Implement canonical Conversation, chat projections, attachment/processing contracts and discriminated events in packages/coire-core/src/coire_core/models/conversation.py, packages/coire-core/src/coire_core/models/chat.py and packages/coire-core/src/coire_core/models/files.py.
- [X] T006 [P] Add compatible text/null/multimodal, backend defaults, visual capability and node/harness/activity schema tests in packages/coire-core/tests/test_multimodal_models.py.
- [X] T007 Extend packages/coire-core/src/coire_core/models/gateway.py, registry.py, engine.py, acquisition.py, harness.py and runs.py with backend/modality/content/activity contracts; add typed failures/settings in packages/coire-core/src/coire_core/errors.py and settings.py without changing existing text semantics.
- [X] T008 Add rows, FKs, unique request/active-turn constraints, processing jobs, quota reservations and text-backend defaults in apps/coire-api/src/coire_api/db.py and one reversible apps/coire-api/alembic/versions/0015_chat_conversations.py; add populated upgrade/downgrade tests in apps/coire-api/tests/unit/test_chat_migration.py.
- [X] T009 Add owner/credential/same-origin and entitlement list/send regression tests in apps/coire-api/tests/contract/test_chat_auth.py and apps/coire-api/tests/unit/test_model_eligibility.py (FR-011, FR-022, FR-033).
- [X] T010 Implement chat-owner authorization and shared entitlement eligibility in apps/coire-api/src/coire_api/auth.py, registry/service.py and gateway/resolution.py; preserve deliberate admin behavior outside Chat and uniform foreign/deleted 404s.
- [X] T011 [P] Add extraction regression tests for existing gateway load/wait/lease/usage/abort behavior in apps/coire-api/tests/unit/test_gateway_execution.py.
- [X] T012 Extract shared inference execution and canonical text adapter into apps/coire-api/src/coire_api/gateway/execution.py, updating routes/v1.py and gateway/usage.py without loopback HTTP or transaction-held engine I/O.
- [X] T013 [P] Add fragmented UTF-8/CRLF/multiline/comment/cursor/auth/terminal/abort and existing-admin regression tests in apps/coire-web/src/api/eventStream.test.ts and hooks/useEventStream.test.tsx.
- [X] T014 Extend apps/coire-web/src/api/eventStream.ts and hooks/useEventStream.ts for typed POST-turn and GET-observer modes, cursor reset and terminal handling; no automatic POST retry, and hidden generation tabs remain connected.

## Phase 3 — US1: Chat and switch models (P1)

**Independent test:** An ordinary user streams a conversation and switches models with no admin access or internal identifiers.

- [X] T015 [P] [US1] Add picker/create/send contracts for published/ready/entitled filtering, metadata and failure/draft preservation in apps/coire-api/tests/contract/test_chat_picker.py and test_chat_turns.py (FR-001–003, FR-007–008, FR-010, FR-022, FR-033).
- [X] T016 [P] [US1] Add ordinary/admin shell routing, empty picker, model selection and stream UI tests in apps/coire-web/src/pages/Chat.test.tsx and components/chat/ModelPicker.test.tsx.
- [X] T017 [US1] Implement locked admission, idempotent request identity, initial messages/model snapshots, variant selection and response/output bounds in apps/coire-api/src/coire_api/chat/turns.py (FR-001, FR-008, FR-010, FR-023).
- [X] T018 [US1] Implement native picker/create/send/status routes and persist-before-emit streaming in apps/coire-api/src/coire_api/routes/chat.py, chat/streaming.py and app.py; include actual usage and safe failures (FR-001–003, FR-007, FR-033).
- [X] T019 [US1] Export native event/request/response schemas through apps/coire-api/src/coire_api/routes/chat.py, regenerate apps/coire-api/openapi.json and apps/coire-web/src/api/schema.d.ts, and add generated-type client functions in apps/coire-web/src/api/chat.ts.
- [X] T020 [US1] Extract apps/coire-web/src/components/AppShell.tsx; implement Chat routing and initial composer/picker/transcript in App.tsx, pages/Chat.tsx, components/chat/Composer.tsx, ModelPicker.tsx, MessageList.tsx and Message.tsx using styles/chat.css and safe Markdown policy (FR-001–003, FR-007–008, FR-010, FR-029–030).
- [X] T021 [US1] Wire sending/model switches/error-preserving drafts through apps/coire-web/src/hooks/useConversation.ts and pages/Chat.tsx, with generated types and shared stream hook (FR-001, FR-008, FR-018, FR-033).
- [ ] T022 [US1] Add local tiny-text streaming/model-attribution integration scenarios in tests/integration/test_chat_ui.py; preserve existing /v1, admin and failover regression coverage (SC-001–002, SC-004).

## Phase 4 — US2: Understand cold-model waits (P1)

**Independent test:** Cold selection shows known/unknown estimate and real status, then streams automatically or explains load failure.

- [X] T023 [P] [US2] Add known/unknown estimate, eviction-after-selection, load failure and queue/status contract tests in apps/coire-api/tests/contract/test_chat_loading.py (FR-004–006, FR-032).
- [X] T024 [P] [US2] Add inline composer cold/queue/failure component tests in apps/coire-web/src/components/chat/Composer.test.tsx (SC-003).
- [X] T025 [US2] Connect actual placement/load transitions and nullable measured estimates to chat events in apps/coire-api/src/coire_api/gateway/loading.py and chat/streaming.py; never manufacture percentages/ranks (FR-004–006, FR-032).
- [ ] T026 [US2] Render warm-up/queue/failure and recovery inside apps/coire-web/src/components/chat/Composer.tsx and ModelPicker.tsx; validate <=1 s status in tests/integration/test_chat_ui.py (SC-003).

## Phase 5 — US3: Persistent private history and recovery (P2)

**Independent test:** Reload preserves attribution/partial output; concurrent tabs cannot overwrite; deletion immediately denies access and purges content.

- [ ] T027 [US3] Add history/update/delete/replay/stop contracts and idempotency/version/race tests in apps/coire-api/tests/contract/test_chat_history.py and tests/unit/test_chat_state.py (FR-009, FR-011, FR-018–019, FR-023–025, FR-035).
- [X] T028 [US3] Implement paginated history/edit/replay, atomic snapshot cursors, latest-attempt prompt selection and explicit retry/continue in apps/coire-api/src/coire_api/chat/service.py, chat/streaming.py and routes/chat.py (FR-009–011, FR-018, FR-023).
- [ ] T029 [US3] Implement idempotent owner stop, original-stream disconnect, terminal/usage races and expired plain-chat lease reconciliation in apps/coire-api/src/coire_api/chat/streaming.py and chat/maintenance.py; observers cannot cancel (FR-019, FR-024, FR-035).
- [ ] T030 [US3] Implement tombstones, content-purge scheduling and event/staging retention in apps/coire-api/src/coire_api/chat/maintenance.py and app.py, with alternate coding artifact deletion guards in mcp_calls.py (FR-025).
- [X] T031 [US3] Implement history/new/delete, version-conflict reconciliation, model snapshots and responsive history drawer in apps/coire-web/src/components/chat/ConversationHistory.tsx and hooks/useConversation.ts (FR-009–011, FR-018, FR-023, FR-030).
- [X] T032 [P] [US3] Add expired-session/draft/identity-change and logout tests in apps/coire-web/src/hooks/useConversation.test.tsx (FR-020, FR-031).
- [X] T033 [US3] Implement owner-scoped same-tab draft restoration and cleanup in apps/coire-web/src/hooks/useConversation.ts and api/chat.ts; preserve text/model/file selections but no tokens or bytes (FR-020, FR-031).
- [ ] T034 [US3] Test and wire hidden-tab preservation, navigation/tab-close cancellation, explicit retry/continue and observer reconciliation in apps/coire-web/src/pages/Chat.test.tsx, pages/Chat.tsx and hooks/useEventStream.ts (FR-018–019, FR-023–024).
- [ ] T035 [US3] Add two-tab/repeated-send, disconnect/restart, deleted access/purge and healthy-stop integration cases in tests/integration/test_chat_ui.py (SC-004–005, SC-008–009, SC-011, SC-013).

## Phase 6 — US4: Safe code mode (P2)

**Independent test:** Research/Plan/Apply run on a stated repository revision, emit real tool activity, obey verification and Stop, and yield owner artifacts with honest tests.

- [X] T036 [US4] Add browser-versus-MCP auth, source/revision/plan binding, owner result/artifact and write-gate contracts in apps/coire-api/tests/contract/test_chat_coding.py (FR-012–013, FR-028).
- [X] T037 [US4] Extract shared coding domain/admission/result/kill operations into apps/coire-api/src/coire_api/coding_calls.py; adapt mcp_calls.py and chat/service.py while retaining MCP's key/scope gate, existing durable records and verified Apply checks.
- [X] T038 [P] [US4] Add actual tool activity, spool bounds, malformed records, cursor and cleanup tests in apps/coire-agent/tests/test_activity.py and apps/coire-node/tests/contract/test_run_activity.py (FR-013).
- [X] T039 [US4] Emit bounded typed started/completed/failed tool records from apps/coire-agent/src/coire_agent/activity.py and coding.py, without raw arguments/content.
- [X] T040 [US4] Implement authenticated run-spool read/validation in apps/coire-node/src/coire_node/runs.py and routes/runs.py; only assigned run output, bounded records and explicit overflow/unavailable state.
- [X] T041 [US4] Collect/deduplicate/drain activity and reconcile durable results in apps/coire-api/src/coire_api/nodes_client.py, run_executor.py and coire_scheduler/runs.py, persisting chat events before cleanup (FR-013, FR-035).
- [X] T042 [US4] Add chat-owned artifact download and tombstone guards in apps/coire-api/src/coire_api/routes/chat.py and coding_calls.py, reusing digest/expiry checks; tests include alternate existing artifact routes (FR-011, FR-025, FR-028).
- [X] T043 [US4] Add action/source/revision controls and live tools/result/diff/test/expiry UI in apps/coire-web/src/components/chat/RunActivity.tsx, Composer.tsx and pages/Chat.tsx, with colocated tests; explicit Apply plan and fresh-workspace notice (FR-012–013, FR-028).
- [ ] T044 [US4] Extend tests/integration/test_chat_ui.py for sandboxed coding, matching plan, refusal of unverified Apply, token revocation/<=5 s kill, API restart resume and verified artifact import (SC-006, FR-028, FR-035).

## Phase 7 — US5: Files, visual inputs and reasoning (P3)

**Independent test:** A user supplies text/code, searchable/scanned PDFs and images; content selection is explicit, eligible models read visuals, unsafe/oversized inputs fail clearly, and reasoning remains separate.

- [ ] T045 [P] [US5] Add upload/processing/original/preview/selection ownership, byte/derived quota races and worker request contracts in apps/coire-api/tests/contract/test_chat_files.py and apps/coire-file-worker/tests/test_contract.py (FR-014–016, FR-026, FR-036–039).
- [X] T046 [US5] Implement scoped worker process/status/cancel/health endpoints in apps/coire-file-worker/src/coire_file_worker/app.py and security.py using core types, immutable IDs/digests/manifests and one active conversion.
- [X] T047 [US5] Implement PDF Unicode extraction/page rasterization, still-image normalization, bounds/metadata stripping/atomic outputs and hard native-call deadline in apps/coire-file-worker/src/coire_file_worker/processor.py; add malformed/encrypted/scan/decompression/crash tests in apps/coire-file-worker/tests/test_processor.py (FR-026, FR-036, FR-038–039).
- [X] T048 [US5] Build hardened apps/coire-file-worker/Dockerfile and add service/private mounts/network/secret/health/resource limits in deploy/compose/compose.yaml; extend scripts/coire-secrets-init.sh, scripts/image-policy.sh and .github/workflows/ci.yml for worker policy/native-link/SBOM/scan gates without broadening existing networks.
- [ ] T049 [US5] Implement upload/download, processing/retry/temporary jobs, selections, private previews and quotas in apps/coire-api/src/coire_api/chat/files.py, chat/processing.py and routes/chat.py; dispatch through DBOS in apps/coire-api/src/coire_scheduler/files.py and main.py using coire_api/file_worker_client.py; resume by manifest status, verify outputs in API before ready publication and test interruption/cancellation (FR-014, FR-016, FR-026, FR-036–039).
- [ ] T050 [US5] Build typed upload/processing/page-selection/private-preview/reuse UI in apps/coire-web/src/components/chat/AttachmentList.tsx, api/chat.ts and hooks/useConversation.ts with component/client tests; no original PDF embedding, silent pages or token-bearing URLs (FR-014–016, FR-036–039).
- [ ] T051 [P] [US5] Add visual content/context/modality/inline-image refusal tests in apps/coire-api/tests/contract/test_chat_vision.py and tests/unit/test_visual_context.py, including plain /v1 compatibility, HTTP/file URL refusal and unsupported Anthropic image blocks (FR-027, FR-037–038).
- [X] T052 [P] [US5] Add VLM explicit argv/offline/remote-code/concurrency/cache/health/reservation/re-adoption/cancel tests in apps/coire-node/tests/contract/test_vision_engine.py (FR-037–038).
- [X] T053 [US5] Implement registry-selected bare VLM lifecycle in apps/coire-node/src/coire_node/engines.py, store.py, reservations.py and routes/engines.py; keep text argv separate and include backend in owned-process discovery/state.
- [X] T054 [P] [US5] Add admin-only preconverted VLM inspection/processor completeness/visual-validation/replication and unsupported recipe tests in apps/coire-api/tests/contract/test_vision_acquisition.py and apps/coire-node/tests/unit/test_vision_validation.py (FR-037).
- [X] T055 [US5] Extend apps/coire-api/src/coire_api/registry/inspection.py, acquisition.py and acquisition_executor.py, plus apps/coire-node/src/coire_node/validation.py and conversion.py, for already-converted supported VLMs and measured visual capability; audit through existing admin acquisition only.
- [ ] T056 [US5] Carry backend compatibility and encoder/cache/image-working-memory reservation through apps/coire-api/src/coire_api/instance/service.py, gateway/resolution.py, placement/service.py and placement/executor.py; reject unsupported old nodes/distributed VLM placement and excess capacity with tests in apps/coire-api/tests/unit/test_vision_placement.py (FR-037–038).
- [ ] T057 [US5] Implement typed visual gateway/context/temporary normalization and principal+digest validated-asset reuse in apps/coire-api/src/coire_api/gateway/execution.py, context.py and routes/v1.py; explicitly reject unsupported images in gateway/anthropic.py and exclude VLM from failover/snapshot.py, with regression tests (FR-015, FR-027, FR-037–038).
- [ ] T058 [US5] Stage immutable multimodal control inputs in apps/coire-node/src/coire_node/workspaces.py and runs.py; propagate visual parts through apps/coire-agent/src/coire_agent/gateway_model.py, context.py, harness.py and coding.py over existing relay, preserving images on retries and verification checks (FR-012, FR-028, FR-037–038).
- [ ] T059 [US5] Test visual coding request preservation, readonly input mount, retry/context limits and verified-write refusal in apps/coire-agent/tests/test_visual_coding.py and apps/coire-node/tests/contract/test_visual_workspace.py (FR-028, FR-037–038).
- [X] T060 [US5] Stage exact locked native engine wheels in scripts/build-node-wheel.sh and consume them in apps/coire-node/install.sh into a new versioned env; test smoke-before-symlink-flip and failed-smoke rollback in tests/unit/test_node_install.py (FR-037, Constitution VII).
- [X] T061 [P] [US5] Add split/unclosed reasoning delimiter, mixed content and cancellation boundary tests in apps/coire-api/tests/unit/test_chat_reasoning.py and test_chat_streaming.py (FR-017, FR-029).
- [X] T062 [US5] Implement profile-driven incremental answer/reasoning parsing and typed channel persistence in apps/coire-api/src/coire_api/chat/reasoning.py and streaming.py (FR-017, FR-029).
- [ ] T063 [P] [US5] Add hostile Markdown/URL/remote-image, code copy, reasoning disclosure and attachment-model-switch tests in apps/coire-web/src/components/chat/Message.test.tsx and ReasoningBlock.test.tsx (FR-017, FR-029, FR-037).
- [ ] T064 [US5] Implement collapsed reasoning, context/visual compatibility remedies and safe attachment attribution in apps/coire-web/src/components/chat/ReasoningBlock.tsx, Message.tsx, ModelPicker.tsx and Composer.tsx (FR-015–017, FR-027, FR-029, FR-037–039).
- [ ] T065 [US5] Add local tiny-VLM image/selected scanned-page streaming/Stop, offline lifecycle and CPU-worker isolation cases in tests/integration/test_chat_ui.py, using runtime-generated fixtures and COIRE_TEST_VISION_MODEL (SC-012, SC-014).
- [ ] T066 [US5] Extend apps/coire-api/src/coire_api/chat/maintenance.py and tests/unit/test_chat_cleanup.py to purge originals/derivatives/temporary inline images/processing leftovers/control inputs and release quota, including deletion during rendering/inference (FR-025–026, FR-035–039).
- [ ] T067 [US5] Align upload-only nginx bounds in apps/coire-web/nginx/nginx.conf and all file-worker/backend environment/resource settings in packages/coire-core/src/coire_core/settings.py and deploy/compose/README.md, with compose/settings contract coverage (FR-026, FR-036–038).

## Phase 8 — Operational proof and release gates

- [ ] T068 Add specified spans/metrics/content-free logs in apps/coire-api/src/coire_api/chat/telemetry.py, coire_scheduler/files.py and runs.py, apps/coire-file-worker/src/coire_file_worker/app.py, apps/coire-node/src/coire_node/otel.py and engines.py, and apps/coire-agent/src/coire_agent/telemetry.py; test privacy/correlation (FR-034).
- [X] T069 [P] Add chat/parser/vision panels and baseline failure/overdue-purge alerts in deploy/observability/grafana/dashboards/chat.json and deploy/observability/alerts/chat.yaml; verify rules and lean/diagnostic behavior (FR-034).
- [ ] T070 [P] Write docs/runbooks/chat-web-ui.md and update deploy/compose/README.md, docs/ARCHITECTURE.md and docs/adr/0008-multimodal-chat-processing.md with inspection/kill/parser failure/purge/diagnostics/locked-node install and audited visual-backend rollback procedures.
- [ ] T071 Regenerate and freshness-check apps/coire-api/openapi.json and apps/coire-web/src/api/schema.d.ts after all routes, event components and visual schemas; verify every new/changed API surface has contract coverage (Constitution III/VII).
- [ ] T072 Run Ruff, strict mypy, unit/contract/worker suites, web tests/lint/typecheck and existing /v1/MCP/admin/failover regressions; record actual results and explain unrelated conditional skips in specs/014-chat-web-ui/review.md.
- [ ] T073 Run quickstart.md browser acceptance on Safari/Chromium at 1024/1440 widths: keyboard/focus/reduced-motion/drawers/auth drafts, unfamiliar-user chat, 200-message/50k-character p95 interaction measurement and visual file flows; save evidence references in specs/014-chat-web-ui/review.md (SC-001, SC-010).
- [ ] T074 Verify migration reversal, compose policy, changed production image builds/scans/SBOMs/native notices, locked node package and broken-env rollback against disposable/local targets; record in specs/014-chat-web-ui/review.md. Real Studio model tests use the authorized admin/node paths; no direct Studio Docker or unmanaged engine commands.
- [ ] T075 Run all local tiny-text/tiny-vision integration scenarios, healthy cancellation/usage/memory/overhead measurements, parser crash recovery and baseline alerts; record model bytes, unskipped 014 results and any operator-only cluster evidence in specs/014-chat-web-ui/review.md before completing acceptance.

## Phase 9 — Provider-agnostic Chat routing (2026-09-29 clarification)

- [ ] T076 [US1] Add strict external-provider/model registration, picker/source and usage contracts in coire-core; add a reversible registry migration and admin audit tests (FR-040).
- [ ] T077 [US1] Implement administrator-controlled OpenAI and Anthropic provider adapters with Keychain-sourced secrets, bounded usage/spend, cancellation, telemetry, dashboard and alert (FR-033–034, FR-040).
- [ ] T078 [US1] Route native Chat and compatible `/v1` generation by registry target while preserving Studio lifecycle, entitlements, history and Code-mode harness placement (FR-012, FR-022, FR-040).
- [ ] T079 [US1] Show source/provider and compatible capabilities in the picker; test switching, attribution, safe unsupported input and Stop (FR-015, FR-037, FR-040).
- [ ] T080 [US1] Run bounded external-provider acceptance with operator credentials and document rollback, cost and privacy controls (SC-015).

## Dependencies and execution order

```text
T001–T003 setup
  -> T004–T014 foundation (tests before their implementations)
  -> US1 T015–T022
      -> US2 T023–T026
      -> US3 T027–T035
          -> US4 T036–T044
              -> US5 T045–T067
  -> T068–T075 complete operational proof
```

T005 follows T004; T007 follows T006; T008 follows core contracts; T010 follows T009; T012 follows T011; T014 follows T013. T017–T021 follow story tests, with T019 before generated web clients. US2 and US3 both require US1; their shared Composer/streaming edits must be serialized. US4 needs US3 owner stop/replay/deletion. US5 parser work and vision-engine work may progress independently after foundation, but integrate only after the earlier chat/coding paths exist. T056 follows T053/T055; T057 follows T049/T053/T055/T056; T058 follows T037/T053/T057; T065 follows full file/vision UI and engine paths. T066 follows all file stores; T068 instruments each implemented path and is finished before operational acceptance. Final gates require all five stories.

## Parallel examples per story

| Story | Independent work after prerequisites |
| --- | --- |
| US1 | T015 backend contract tests and T016 web tests; gateway tests T011 and transport tests T013 in foundation. |
| US2 | T023 backend loading tests and T024 composer tests. |
| US3 | T032 draft tests while T027 backend tests are authored; implementation touching useConversation stays sequential. |
| US4 | T038 agent/node activity tests and T036 coding admission tests. |
| US5 | T045 files, T051 gateway vision, T052 node lifecycle, T054 acquisition, T061 reasoning and T063 rendering tests touch separate files. File-worker T046–T049 and VLM T052–T056 can proceed separately before integration. |

T069 and T070 may run in parallel after telemetry names/operational behavior are settled. Shared schema generation, migrations, dependency lockfiles, compose and shared hooks are serialized.

## Implementation strategy and completion

First demonstrate US1 with its secure typed foundation. Add cold state, history/recovery, coding, then confirmed multimodal attachments. Keep tests with each logical change and never mark skipped acceptance as passed. Contract/schema and node compatibility checks gate use of new backend features. Review-sized delivery must obey CONTRIBUTING.md; split specifications before implementation if the feature cannot fit a reviewable change, keeping this umbrella acceptance scope intact. No task permits automatic production deployment or unapproved network/firewall broadening.

The feature is complete only after all five stories and T068–T075 pass, with the template PR linking spec/ADR, dependency licences, observability, runbook and real validation evidence. Planning completion does not check any box here.

## Requirement traceability

| Requirement | Implementation / acceptance tasks |
| --- | --- |
| FR-001 | T005, T012, T015, T017–T022 |
| FR-002 | T009–T010, T015–T016, T018, T020 |
| FR-003 | T015–T016, T018, T020, T025 |
| FR-004 | T023–T026 |
| FR-005 | T023–T026 |
| FR-006 | T023–T026 |
| FR-007 | T015, T018, T020 |
| FR-008 | T017, T020–T022 |
| FR-009 | T008, T027–T028, T031, T035 |
| FR-010 | T017, T020, T022, T028, T031 |
| FR-011 | T009–T010, T027–T028, T035–T036, T042, T045 |
| FR-012 | T036–T044, T058–T059 |
| FR-013 | T036–T044 |
| FR-014 | T045–T050, T065–T067 |
| FR-015 | T049–T051, T057, T064–T065 |
| FR-016 | T045–T050, T064–T065 |
| FR-017 | T061–T065 |
| FR-018 | T013–T014, T021, T027–T029, T031, T034–T035 |
| FR-019 | T027, T029, T034–T035 |
| FR-020 | T032–T033, T073 |
| FR-021 | T020, T073 |
| FR-022 | T009–T010, T015–T016, T020, T036, T045 |
| FR-023 | T008, T017, T027–T028, T031, T034–T035 |
| FR-024 | T029, T034–T035, T037, T044 |
| FR-025 | T030, T035, T042, T049, T066, T069–T070 |
| FR-026 | T045–T050, T065–T067 |
| FR-027 | T051, T057, T064–T065 |
| FR-028 | T036–T037, T042–T044, T058–T059 |
| FR-029 | T002, T020, T061–T064 |
| FR-030 | T020, T031, T073 |
| FR-031 | T032–T033, T073 |
| FR-032 | T023–T026 |
| FR-033 | T009–T012, T015, T018, T021, T072, T075 |
| FR-034 | T068–T070, T075 |
| FR-035 | T029–T035, T037, T041, T044, T049, T065–T066 |
| FR-036 | T045–T050, T064–T067 |
| FR-037 | T002–T007, T050–T060, T064–T067, T074–T075 |
| FR-038 | T045–T049, T051–T060, T064–T067, T075 |
| FR-039 | T045–T050, T064–T066 |
| SC-001 | T022, T073 |
| SC-002 | T009–T010, T015–T016, T022 |
| SC-003 | T023–T026 |
| SC-004 | T022, T027–T028, T035 |
| SC-005 | T009, T027, T035, T036, T045 |
| SC-006 | T036–T044 |
| SC-007 | T061–T065 |
| SC-008 | T013–T014, T027–T029, T034–T035 |
| SC-009 | T017, T027–T028, T031, T035 |
| SC-010 | T020, T031, T073 |
| SC-011 | T027, T030, T035, T042, T045, T066, T075 |
| SC-012 | T045–T067, T075 |
| SC-013 | T027, T029, T035, T075 |
| SC-014 | T051–T060, T065, T074–T075 |

T001–T014 also trace to Constitution III/IV/V/VII; T068–T075 cover II-a/III/VI/VII operational gates. No setup or release task is unrelated scope.
