# Implementation Plan: Chat Web UI

**Branch**: `feat/014-chat-web-ui` | **Date**: 2026-09-28 | **Spec**: [spec.md](spec.md)

## Summary

Add ordinary-user chat to the Glass SPA with private history, truthful readiness, streaming answers/reasoning, text/code/PDF/image attachments and existing coding actions. User-confirmed scope includes scanned PDFs. Add an isolated CPU file worker on core and direct bare MLX-VLM inference on Studios; reuse gateway admission/accounting and run orchestration. Start with shared contracts, persist turns/events, and consume typed events through the shared hook.

## Technical Context

**Language/Version**: Python 3.13; TypeScript 5.9.3 resolved by the lockfile; React 18.3.1.

**Primary Dependencies**: Existing FastAPI/Pydantic/SQLAlchemy/Alembic/httpx/OTel, DBOS only in scheduler, React/Vite. Add `react-markdown==10.1.0` (MIT), worker-only `pypdfium2==5.13.0` (Apache-2.0/BSD-3-Clause plus PDFium notices) and `Pillow==12.3.0` (MIT-CMU), Studio-only `mlx-vlm==0.7.3` (MIT). Pin transitive dependencies, preserve notices and verify actual worker arm64 linking; core images never contain MLX-VLM/weights.

**Storage**: PostgreSQL 17; `coire-chat-files` original volume (API write/worker read-only) and `coire-chat-derived` output volume (worker/API write under generated job keys); identity-scoped same-tab draft storage. Existing coding artifact store. One reversible migration after `0014_mcp_calls`, including processing jobs and registry backend/capability fields.

**Testing**: pytest contract/unit/local integration including parser isolation and visual lifecycle; Vitest/Testing Library; manual Safari/Chromium acceptance. Tiny text and vision fixtures each <=1 GB. On `coire-core.lab`, no weights or Metal work may run; real native checks use the Studios through the admin acquisition and node paths as directed by the operator, without direct Docker or unmanaged engine commands. External-provider acceptance uses a bounded paid-call budget and never writes credentials to the repo.

**Target Platform**: Core API/web plus new one-process CPU file-worker container; Studio node/agent and bare text/vision engines. Desktop 1024–1920 px with 1024/1440 acceptance. Failover stays stateless and text-only; exclude unsupported vision residents from failover snapshots until separately supported.

**Performance Goals**: Warm status <=1 s; observer reconciliation <=2 s; healthy Stop <=5 s; interaction p95 <=100 ms for 200 messages + 50,000-character response. Maintain gateway overhead <=20 ms p95 excluding model time. Browser reference: Apple Silicon development Mac without throttling; record machine/browser versions.

**Constraints**: Owner-scoped content, one active turn/conversation, no automatic POST replay, no core model/tokenizer/harness, no acquisition via chat, generated web types, bounded files, OpenAI compatibility for Studio models, native-Chat-only registry-selected external providers and Keychain-sourced credentials. A configured default Chat model is a registry UUID ordered first in the eligible picker, with automatic fallback when it is no longer eligible.

**Administrator management extension**: Chat presents the existing core-only `coire-ops` read/propose workflow only to administrators. Its pinned Sonnet option keeps the ops container on internal networks: a scoped `ops:infer` credential calls a narrow API relay, and only coire-api holds the Keychain-sourced Anthropic key and provider egress. The relay fixes the remote model, bounds output/body/tool names, and never grants confirmation authority. The human admin proposal endpoint performs exact confirmation, precondition checks and audit as before. The Studio-backed ops option remains available for rollback.

**Scale/Scope**: Three nodes; history pages default50/max100; 10 MiB originals, 50 MiB/conversation and 500 MiB/owner including derivatives; ten files/visual units per turn; PDF <=50 pages, upload <=20 MP, normalized <=2048 px/side and <=4 MP; extraction <=1 MiB and derivatives <=32 MiB/job. 64 KiB input/512 KiB response; events24h, history until deletion. No retrieval/shared editing/persistent coding workspace/full settings/image generation/feedback; visual serving is single-node and preconverted MLX models initially.

## Constitution Check

*Pre-research and post-design: PASS against constitution 2.0.0. Implementation evidence remains unchecked in tasks.md.*

| Principle | Compliance |
| --- | --- |
| I — Bare engines | Direct `mlx_lm.server` and `mlx_vlm.server`, with explicit backend argv and node lifecycle. VLM is a bare engine, not an inference wrapper. Record the roster addition in an ADR. |
| II — Core/worker boundary | Local learned text/vision inference and user harnesses stay on Studios. Core API may proxy approved external HTTPS providers; core file-worker performs CPU parsing/rasterization only, with no model, tokenizer, Metal or user harness. |
| II-a — Containers | File-worker gets its own distroless non-root one-process image, read-only rootfs, caps dropped, healthcheck, 512 MiB/1 CPU and scheduler-only processing network. Existing roles retain policy; no API parser subprocess. |
| III — Contracts first | Strict canonical/native/activity models precede services; generated OpenAPI/TS; compatible `/v1` and MCP. |
| IV — Zero trust | User-bound chat scope, ownership even for admins, exact same-origin mutations, current entitlements/budgets, short-lived run tokens and owner kill; audits exclude content. |
| V — Models as data | Published/ready/entitled registry picker and send, including provider target data; verified local variant required for Apply; no acquisition or arbitrary endpoint path in Chat. |
| VI — Observable | Spans, metrics, content-free logs, panel and baseline alerts; diagnostic history follows lean/diagnostic profile. |
| VII — Spec/test gated | Contract/unit/web/tiny-model/browser acceptance, migration and compatibility checks, image policy/scan/SBOM gates. |

Post-design: no constitution exception. An ADR and architecture update document the added bare VLM engine and isolated CPU file-worker; this preserves existing principles. Eligibility disagreement and admin-only shell are integration gaps in this feature.

## Project Structure

### Documentation

`specs/014-chat-web-ui/{spec.md,plan.md,research.md,data-model.md,quickstart.md,tasks.md,checklists/requirements.md,contracts/chat-api.md}`.

### Source changes during implementation

| Concern | Paths |
| --- | --- |
| Contracts | `packages/coire-core/src/coire_core/models/{conversation,chat,files,gateway,registry,engine,acquisition,harness,runs}.py`, `errors.py`, `settings.py`; core tests |
| Persistence | `apps/coire-api/src/coire_api/db.py`, `apps/coire-api/alembic/versions/0015_chat_conversations.py` |
| Chat | `apps/coire-api/src/coire_api/chat/{service,streaming,files,reasoning,maintenance,telemetry}.py`, `routes/chat.py`, `app.py` |
| Shared inference | `coire_api/gateway/{execution,resolution,context,loading,usage}.py`, `registry/service.py`, `routes/v1.py`, `auth.py` |
| Coding/activity | `coire_api/{coding_calls,mcp_calls,runs,run_executor,nodes_client}.py`, `coire_scheduler/runs.py`; `coire_node/runs.py`, `coire_node/routes/runs.py`; `coire_agent/{coding,activity,gateway_model,context,harness}.py` |
| Visual engine/acquisition | `coire_node/{engines,validation,conversion,workspaces}.py`, API `registry/{inspection,acquisition,acquisition_executor}.py`, `instance/`, `gateway/`; `apps/coire-node/{pyproject.toml,install.sh}`, `scripts/build-node-wheel.sh`, `uv.lock` |
| File worker | `apps/coire-file-worker/{pyproject.toml,Dockerfile,src/coire_file_worker/{app,processor,security}.py,tests/}`; `coire_api/chat/processing.py` |
| Processing workflow | `apps/coire-api/src/coire_scheduler/files.py`, `coire_scheduler/main.py`, `coire_api/file_worker_client.py`; DBOS workflow only in scheduler |
| Web | `apps/coire-web/src/{App.tsx,components/AppShell.tsx,pages/Chat.tsx,api/chat.ts,api/eventStream.ts,hooks/useEventStream.ts,hooks/useConversation.ts,styles/chat.css}`; `components/chat/{ConversationHistory,ModelPicker,MessageList,Message,Composer,ReasoningBlock,AttachmentList,RunActivity}.tsx` |
| Tests | Core tests; API `tests/{contract,unit}/test_chat_*.py`; node/agent activity tests; `tests/integration/test_chat_ui.py`; colocated web tests |
| Operations | `deploy/compose/{compose.yaml,README.md}`, `apps/coire-web/nginx/nginx.conf`, `deploy/observability/{alerts/chat.yaml,grafana/dashboards/chat.json}`, `docs/runbooks/chat-web-ui.md` |

## Phase 0 — Research and clarification

[research.md](research.md) records current code, primary-source dependency evidence and alternatives. Two answered questions confirm text/code/PDF/images and scanned PDFs. One active turn, explicit retries, fresh coding snapshots and blocked plain-chat overflow remain documented defaults. No unresolved technical choice blocks tasks.

## Phase 1 — Design

### Execution and history

Canonical content is independent of persisted UI metadata. Admission validates identity/model/context/files, locks the conversation, deduplicates request ID and checks revision, then persists user/assistant/turn/event before starting. Reuse extracted gateway execution for resolution, limits, engine leases, cancellation and once-only usage; no transaction stays open during engine I/O.

Persist output before emission; cursor replay plus message snapshots preserve partial history. Only the original sending stream controls generation. Stop records intent and releases plain inference or revokes/queues coding kill; terminal state follows acknowledgement. API process loss expires plain-chat leases as interrupted; coding reconciles its durable run. Idempotent API lifespan maintenance sweeps stale turns, event retention, tombstones and abandoned uploads from persisted state on startup and bounded intervals; no new DBOS workflow or queue in the API.

### Models, loading and context

Shared eligibility uses principal entitlements, with published+ready filtering for every Chat user. Resolve consistent registry variants across picker/send/write gate; show size class and honest unknown estimates. Stream actual scheduler/loading state, without invented queue ranks or percentages. Refresh the picker on open and reauthorize each send.

Compile full history, page-attributed text and ordered image parts; retain model/name snapshots. Text preflight includes framing/Unicode/output; visual budgeting uses measured image-token/pixel/count/working-memory limits rather than base64 length. No weights/tokenizers on core; backend preflight remains authoritative. Incompatible model switches and overflows are refused visibly. Profile-driven reasoning parsing handles split/unclosed tags without answer leakage.

### Files and deletion

Reserve original and derivative quota under lock, stream originals to generated keys and persist processing jobs. API enqueues a typed processing job; a DBOS workflow in coire-scheduler dispatches authenticated work to `coire-file-worker`, which reads originals read-only and writes bounded generated outputs. One conversion at a time serializes PDFium. Worker watchdog enforces a 30-second deadline by terminating its sole process; OOM/native crash isolates to that container. Scheduler detects death and records failure; API removes partial assets and offers at most two explicit attempts for unchanged input. No automatic crashing-document loop.

PDFium extracts Unicode with page attribution and renders selected scan/diagram pages; Pillow validates/normalizes still images, strips derived metadata and makes safe raster previews. Text/visual PDF mode and page selection are explicit before Send. Oversized outputs fail with guidance; no silent omissions. Scheduler persists the worker manifest in a processed state; API verifies output IDs/digests/quota and atomically publishes ready metadata/events. The DBOS workflow resumes by querying the job's immutable manifest after restart, never blindly rerendering a failed document. A missing interrupted result becomes a visible failure. Tombstone cancels processing/generation, denies access and initiates cleanup; eventual outputs for deleted jobs are discarded. API reauthorizes downloads/previews and purges originals/derivatives/content within 24 hours; quota releases after deletion.

### Bare visual inference

Add `EngineBackend` and measured modality/visual limits through registry, variant, instance, acquisition, node start/status and re-adoption. Default existing records to mlx_lm/text. Admin acquisition initially admits supported preconverted MLX-VLM repositories, validates local processor/config files and a real visual smoke, replicates both Studios and then allows publication. Unsupported conversion/distributed recipes fail explicitly. No user request downloads anything.

Launch `python -m mlx_vlm.server` with explicit local model path, host/port, bounded sequences/vision cache/KV. Its argv differs from mlx-lm; v0.7.3 has no `--chat-template`. Set offline mode, remove HF tokens and inherited remote-code trust; never pass remote URLs/files as image inputs. Reserve encoder/cache/image working memory in existing placement/request leases. Add health/cancel/stop/re-adoption tests and preserve text backend behavior.

Extend standard OpenAI content arrays in gateway/node and canonical/harness parts. Native Chat resolves private assets to bounded normalized data URIs. Public inline data images use the same isolated processing via temporary jobs; HTTP/file URLs are refused. Cache validated normalized assets by principal plus digest/schema version to avoid repeatedly parsing images in a coding loop, with ownership/expiry checks. `/v1/messages` remains text-compatible and explicitly refuses unsupported image blocks. Failover snapshot excludes unsupported visual backends.

Stage locked MLX-VLM and transitive wheels in a new versioned node environment; smoke before flipping the existing symlink, rollback on failure. Update installer/build staging so it consumes exact dependencies instead of `--upgrade`. This is required reproducibility for the new backend, not the feature-019 upgrade UI.

### Coding and live tools

Extract shared coding operations, preserving MCP's key/scope gate, and add the chat owner adapter. Reuse approved source/revision, clones, results, verification, tokens/limits/artifacts. Plans match repository/revision. Carry extracted text and normalized visual parts in a dedicated read-only control-input mount, through actual `GatewayTransport` and existing relay, preserving them across retries. Visual coding needs visual and coding capability; Apply still needs verification. Each action starts from its stated revision and never pushes.

Actual tool invocations write typed activity into a bounded NDJSON spool on the existing output mount. Node validates and reads through an additive authenticated route; scheduler/executor collect each second, deduplicate run+sequence, persist owner events and drain before cleanup. Overflow is visible. This needs no new network, relay route, port or Docker capability.

### Browser

Extract a role-aware `AppShell` and Chat routing without ordinary-user access to admin snapshots. Preserve failover entry. Use existing History API and tokens. `useConversation` owns drafts, selection and revision errors; all networking lives in `src/api`, with generated types. Extend `useEventStream` with POST-turn and GET-observer modes while retaining admin snapshots. Reset cursors on URL change, stop retries on terminal/401, keep hidden generation streams open, and recover failed POST state with GET before offering retry/continue.

Owner-scoped session storage retains draft text/model/file/page selections through reauthentication, clears on logout/identity change and never stores credentials/bytes. Attachment UI shows processing, safe previews, page count/selection, PDF text/visual mode and compatible-model guidance. Restore focus; `Cmd/Ctrl+Enter` sends and Escape closes popovers. Announce state transitions, preserve reading position/reduced motion. Stable keys/memoized bubbles reduce rerenders; measure acceptance.

### Observability and settings

Spans: `coire.api.chat.admit`, `.stream`, `.stop`, `.upload`, `.purge`; `coire.file_worker.process`; `coire.node.vision.load`; `coire.scheduler.chat.activity`; `coire.node.run.activity`; `coire.agent.coding.tool`. Metrics: `coire_chat_turns_total{mode,outcome}`, `coire_chat_active_turns`, `coire_chat_stop_seconds`, `coire_chat_upload_rejections_total{reason}`, `coire_chat_purge_oldest_seconds`, `coire_file_processing_total{outcome}`, `coire_vision_requests_total{outcome}`. Correlation includes user/model/run/job/conversation/turn IDs; no content/high-cardinality metric labels.

Dashboard: chat/vision/parser failures, active/interrupted turns, stop latency and upload refusals. Baseline alert `CoireChatFailuresHigh`: non-user failure ratio >10% over ten minutes with >=20 turns. `CoireFileProcessingFailuresHigh`: worker-internal failures >25% over ten minutes with >=10 jobs, excluding user file refusals. Also alert on pending purge older than 24 hours. Diagnostic availability follows the active profile and is explained in the runbook.

Settings in core/compose README: `COIRE_CHAT_ENABLED`, `COIRE_CHAT_PUBLIC_ORIGIN`, `COIRE_CHAT_FILE_ROOT`, `COIRE_CHAT_DERIVED_ROOT`, `COIRE_CHAT_FILE_MAX_BYTES`, `COIRE_CHAT_CONVERSATION_FILE_MAX_BYTES`, `COIRE_CHAT_USER_FILE_MAX_BYTES`, `COIRE_CHAT_TURN_MAX_ATTACHMENTS`, `COIRE_CHAT_EVENT_RETENTION_SECONDS`, `COIRE_CHAT_PURGE_DEADLINE_SECONDS`, `COIRE_CHAT_UPLOAD_STAGING_TTL_SECONDS`, `COIRE_FILE_WORKER_URL`, `COIRE_FILE_WORKER_TIMEOUT_SECONDS`. Worker credential comes only from its dedicated Keychain-sourced compose secret. Validated document/image/derived bounds and backend limits follow data-model and cannot exceed deployed worker resources. Scheduler and worker alone join a new per-concern processing network; no existing network/firewall/CORS setting is broadened. Deployment initializes volume ownership, adds worker health/image policy/CI/SBOM checks and keeps runtime images shell-free.

## Delivery and gates

Contracts → persistence/auth/transport → US1 chat → US2 cold state → US3 history/recovery → US4 coding → US5 files/visual serving/reasoning → operational proof. US5 extends the US4 harness path for screenshots after visual contracts/lifecycle exist. Each story has independent acceptance; US1 plus foundation is the first demo, not feature completion.

Generate OpenAPI using `uv run python -m coire_api.openapi`, then TS from `apps/coire-api/openapi.json`. Verify reversible migration and `/v1`/MCP/admin/failover compatibility. Run tiny-model integration locally and record model size/outcomes. Every acceptance task remains unchecked until executed; unrelated conditional skips require accounting and 014 acceptance cannot pass via skips. Build/policy/CVE/SBOM-check changed images and package-check the node wheel. Record browser and operational evidence before merge.

## Complexity Tracking

No constitution exceptions. Event persistence serves partial recovery/concurrent tabs; a separate file worker isolates native parser resources while honoring one process per service; a second bare engine is required for user-confirmed visual input. Record those architecture additions in a scoped ADR. Existing coding tables retain names plus an origin discriminator.
