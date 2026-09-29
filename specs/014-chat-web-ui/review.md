# Feature 014 implementation review

## Gated inline visual preflight (014bx, 2026-09-29)

- Registry resolution now carries the selected model's backend and measured visual capability. With the new `GATEWAY_INLINE_VISUAL_ENABLED` flag, the compatible `/v1` route checks a PNG data URI against measured image count, byte and pixel limits before engine I/O and conservatively reserves visual context. The flag defaults `false`; text models and unsupported images still fail closed. Parent T057 remains open for file-worker normalization, digest-scoped asset reuse, native Chat images and complete context handling.
- Full Python suite: 1,112 passed, 119 conditional integration skips. Focused context and compatible route contracts: 20 passed. Ruff, strict mypy on 241 source files, OpenAPI freshness, the prior 84 web tests/lint/build and clean diff passed. The changed arm64 API image passed seven image policy rules and Trivy CRITICAL scan with zero findings; Syft wrote `/tmp/coire-api-014bx.spdx.json`.
- In pre-prod, the flag was temporarily enabled through `coire-up --build`; a managed Studio VLM instance reached ready and a valid 90-byte/16×16 red PNG sent through core `/v1/chat/completions` returned HTTP 200, answer `Red.`, and 1,145 prompt plus 3 completion tokens. The instance drained to stopped through the API. `coire-up --no-build` restored the default flag; the live API reports it as `false` and rejects the same image with HTTP 400. No engine or model ran on core.

## Native Studio acquisition acceptance (2026-09-29)

- An audited temporary admin API key was issued through the existing identity service, stored only in the login Keychain, and accepted by the pre-prod API. The key has admin scope, a 60-request/minute limit and a 100,000-token monthly budget. Revoke key `3ae0bcb9-09cd-4a04-84f0-62422836dc92` and delete Keychain service `coire-admin-api-key` after the remaining live acceptance work. No credential was printed or added to Git.
- The live admin acquisition workflow `6a5d2714-e8d7-485f-b445-c4c72d4d9863` acquired `mlx-community/SmolLM-135M-Instruct-4bit` (79,217,711 total bytes). Its first validation exposed a false tool-template requirement for a plain chat template. After the node fix and audited admin retry, validation and replication succeeded; registry model `44359c95-8f49-4d8a-85d3-a034af6c138b` is ready with two verified Studio copies and remains admin-only.
- A private pre-upgrade PostgreSQL dump was saved outside Git at `~/.coire/projects/coire/backups/pre-014-2026-09-29.dump`. `COMPOSE_PROFILES=mcp deploy/compose/coire-up --build` upgraded the pre-prod core control plane successfully; API, scheduler, MCP, web and database were healthy afterward. Both Studios received locked node environments through `scripts/build-node-wheel.sh` and the staged installer. The existing text engine on edge-a remained running through its node-agent restart.
- The live admin workflow `af76c74f-4d85-4135-970f-dc8660301d89` acquired `mlx-community/SmolVLM-256M-Instruct-4bit` (150,417,043 total bytes) on edge-b. Native validation found two genuine compatibility issues: Transformers 5.16.1 required its Torchvision image-processor backend, and the fixed smoke prompt needed an `<image>` marker. The node-only locked dependency adds Torchvision 0.29.0 and Torch 2.14.0 (BSD-style licences); neither belongs in a core image. After the fixes and admin retries, visual generation passed, the checksum-matched replica was stored on edge-a, and model `b3a9d66c-fd70-44d0-957a-ef6a3e63e3aa` is ready, verified and admin-only. Measured visual encoder peak was 936,487,174 bytes and cache was 123,984,430 bytes. The smoke used a 16×16 image, so its published 256-pixel/90-byte input limit is deliberately narrow; larger image/chat lifecycle acceptance remains open. Parent T054/T055 and child 014bu J003 are complete; T052/T053/T065/T075 remain open.
- Post-change gates: 1,110 Python tests passed with 119 conditional integration skips; Ruff, strict mypy on 241 source files and the changed node tests, OpenAPI freshness, all 84 web tests, web lint and build passed. The repository-wide mypy command reported 200 test typing errors in 16 files; that broader gate is not green. Model weights and Metal execution stayed on Studios. No direct Studio Docker or unmanaged engine command was used.
- Both Studios now run the same 74-wheel locked native node environment and report healthy. Via authenticated instance APIs, the visual variant reached `ready` on edge-b, while a tiny text instance reached `ready` there and served an OpenAI-compatible `/v1/chat/completions` request with HTTP 200 and usage (`16` prompt, `24` completion tokens). The tiny 135M text output was low quality; this smoke proves routing/accounting, not answer quality. Both test instances drained to `stopped` through the API, releasing their managed engines. Existing edge-a text serving survived the node rollout. Parent T052/T053 are complete for bare visual lifecycle and contracts.
- The same `/v1` API explicitly returned HTTP 400 `inline image processing is not available yet` for a valid 90-byte/16×16 PNG on the verified visual model. This is the known T057/T065 gap: visual request normalization/context and end-to-end image streaming still need implementation. The temporary key was expanded through the audited admin API to `admin` and `chat` scopes for this smoke; its rate and monthly token budget stayed bounded.

## Provider and Studio acceptance clarification (2026-09-29)

- The operator confirmed that the core-hosted Chat interface must route to entitled Studio models and frontier providers. Parent FR-040/SC-015 and child 014bw track external-provider registration, streaming, Stop, usage, picker labeling and bounded acceptance. The rollback-only Keychain admin token returned 401 from the live API; an audited temporary key resolved admin access as recorded above. Both Studio nodes are reachable with scoped node credentials.

## Visual node startup guards (014bv, 2026-09-29)

- Before a VLM process starts, coire-node now checks complete unlinked local processor files and re-hashes the stored copy against its manifest. Its bare engine environment strips `HF_TOKEN`, `HF_API_TOKEN` and `HUGGING_FACE_HUB_TOKEN`; offline mode and remote-code refusal remain. Mock node contracts cover corrupt/linked copy refusal, budget and option bounds, cancellation and backend identity. Parent T052/T053 stay open for a real Studio lifecycle run.
- Full Python suite: 1,109 passed, 119 conditional integration skips. The eight focused visual engine contracts, Ruff, strict mypy on 241 source files and the changed test, all 84 web tests plus lint/build, and local-only locked node wheel staging passed. No model or Metal work ran on core.

## Tiny-model disposable integration (014bu, 2026-09-29)

- The local `coire-it` Compose topology ran the admin acquisition pipeline with the tiny raw Llama text model and the pinned `mlx-community/SmolVLM-256M-Instruct-4bit` visual model. Both test requests went through the admin API; the text pipeline reached two verified copies. The visual pipeline pulled the real model, then failed at validation on the Linux test node and remained unvalidated/unready, as required when native MLX-VLM is absent. The two targeted integration tests passed in 143 seconds after rebuilding the stale disposable migration image. All `coire-it` containers, networks and model-bearing volumes were removed afterward.
- The active host is `coire-core.lab`. Principle II prohibits loading model weights or starting Metal here, so the real visual smoke and complete native vision acquisition acceptance remain open. An authorized separate Apple Silicon development host is needed; no real Studio was contacted. Parent T054/T055 remain open. The 014bu child J003 remains open.
- The repository gate after adding the integration test passed: 1,106 Python tests, 119 conditional integration skips, Ruff, strict typing for the new test, and a clean diff. `.env.local` is ignored and was neither printed nor staged.

## Admin visual acquisition publication gate (014bt, 2026-09-29)

- Supported preconverted Idefics3 sources now enter the audited admin acquisition workflow when their inspected quantization matches the requested recipe. Mismatches fail before transfer. The scheduler carries the inspected backend to node validation and requires a passing result with measured verified visual capability; final publication rechecks that result and matching origin/replica manifests before storing backend and visual limits in the model and variant registry. Parent T054/T055 remain open pending a real tiny-model local acquisition and end-to-end acceptance.
- Full Python suite: 1,106 passed, 118 conditional integration skips. The focused admin/scheduler tests passed, as did Ruff, strict mypy on 241 source files, OpenAPI freshness, and all 84 web tests plus lint/build. Local OrbStack arm64 API and scheduler images built, passed image policy and Trivy CRITICAL scans with zero findings; Syft SBOMs are at `/tmp/coire-api-014bt.spdx.json` and `/tmp/coire-scheduler-014bt.spdx.json`. No real engine or Studio was contacted. The visual candidate's official config reports Idefics3 and 4-bit group-size-64 quantization.

## Local vision validation gate (014bs, 2026-09-29)

- The node validation request now carries a strict core `backend` selection. A visual job rechecks the local checksum manifest and complete, unlinked processor and weights, then loads only its local store path with the pinned bare MLX-VLM loader, remote code disabled and Hub lookups offline. It generates a runtime 16×16 pixel image and requires nondegenerate output. Pass results include measured visual limits; text perplexity is `not_comparable`. Failures are typed and cannot mark the variant validated. The existing text job remains the default. The admin visual acquisition refusal remains active; parent T054/T055 are still open until scheduler publication and local tiny-model acceptance pass.
- Full Python suite: 1,103 passed, 118 conditional integration skips. Focused node tests: 27 passed. Web: 84 tests, lint and build passed. Ruff, strict mypy on 242 source/test files, OpenAPI freshness, and generated TS types passed. Local-only node wheel staging built 68 locked macOS arm64 wheels without contacting a Studio. The local OrbStack arm64 API image `coire-api:014bs` built, passed seven image policy rules and a Trivy CRITICAL scan with zero findings; Syft wrote `/tmp/coire-api-014bs.spdx.json`. No real engine or Studio was contacted.

## Preconverted vision inspection gate (014br, 2026-09-29)

- Metadata inspection recognizes only the pinned bare VLM Idefics3 family when already converted to MLX and accompanied by local config, processor, preprocessor, tokenizer and safetensors files. Raw/incomplete sources and other visual architectures receive safe typed refusals. A complete visual source still receives an audited `vision_validation_unavailable` 422 with zero bytes transferred until node visual smoke and scheduler publication are wired. This prevents the text validator from falsely marking a VLM ready. Parent T054/T055 remain open; no tiny vision model was acquired.
- Full Python suite: 1,098 passed, 118 conditional integration skips. All 84 web tests, lint and build, Ruff, format, strict mypy on 240 source files plus the new test module, and OpenAPI freshness passed. The local arm64 API image built, passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014br.spdx.json`. No Studio or engine was contacted. Chat remains default-off.

## Parent history acceptance audit (2026-09-29)

- T028 is complete in the existing service, turn and observer paths: owner-filtered stable history paging, versioned edits, a read-locked snapshot cursor, persisted event replay, latest assistant attempt in later prompts, and explicit retry/continuation with unchanged input rules. The corresponding contract and unit cases passed in the full 1,091-test Python run. T027 remains open for the dedicated cross-operation race coverage in its test task.
- T031 is complete in the existing browser path: new/open/rename/delete history actions, 409 refresh reconciliation, per-turn model attribution, older-message paging and a responsive drawer. The 84-test web run includes history, mutation, draft separation and model snapshot cases. Browser interaction acceptance remains T073; multi-tab and tab-close acceptance remains T034/T035.

## Shared text execution slice (014bq, 2026-09-29)

- The compatible OpenAI route and native Chat now serialize text messages through shared bare-engine payload adapters, and compatible cold loading resolves the registry model through the gateway execution module. Both paths use the same bounded load operation and once-only usage binding. Compatible routes release read transactions before cold loading and before warm/cold engine I/O, and recheck run credentials after loading. Native Chat resolves in short-lived sessions while retaining owner checks, durable status events and Stop behavior. No loopback HTTP was introduced. Protocol-specific admission remains in the routes and Chat turn service. Parent T012 is complete.
- Full Python suite: 1,091 passed, 118 conditional integration skips. Focused gateway/native loading cases: 52 passed. Web: 84 tests, lint and production build passed. Ruff, format, strict mypy for 240 source files and OpenAPI freshness passed. Chat remains default-off.
- The changed local arm64 API image built, passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014bq.spdx.json`. This was local OrbStack only, with no Studio contact.

## Code controls slice (014bp, 2026-09-29)

- Added action-aware coding model picker, strict Chat turn coding-call identity, browser Research/Plan/Apply controls, owner workspace registration and exact-origin protection for browser workspace mutations. Plan/Apply inherit recorded source revisions; Apply requires an explicit selected plan. Code result UI displays bounded tool activity, tests, diff and owner artifact expiry before download.
- Focused core/API contracts: 18 passed. Repaired-environment full Python suite: 1,088 passed and 118 conditional integration skips. Final web suite: 84 passed; TypeScript production build and lint passed. Strict mypy passed for 240 source files; Ruff, format and OpenAPI freshness passed. The 118 skips remain conditional local integration scenarios, not 014 acceptance evidence.
- Parent T043 and child J005 are complete. Browser acceptance and the parent local tiny-model integration tasks remain open. Chat remains default-off.
- The changed local arm64 web image built, passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-web-014bp.spdx.json`. This was local OrbStack only, with no Studio contact.

## Baseline (2026-09-28)

- Base revision: `55ca455a5403d00fb8b0736a4ec1bd9393b3adbb` on `feat/014-chat-web-ui`.
- Alembic head: `0014_mcp_calls`; feature migration begins at `0015_chat_conversations`.
- `uv run ruff check .`: pass.
- `uv run mypy packages/coire-core/src apps/coire-api/src apps/coire-node/src apps/coire-agent/src apps/coire-failover/src`: pass (212 source files). The bare `uv run mypy` command has no targets configured and exits 2.
- `uv run pytest -q`: 801 passed, 116 skipped, 255 warnings. Skips are pre-existing integration/engine scenarios and are not 014 acceptance evidence.
- `pnpm -C apps/coire-web test`: 19 passed. `pnpm -C apps/coire-web lint` and `pnpm -C apps/coire-web build`: pass.
- `checklists/requirements.md`: 16/16 checked. No extension hooks configured.

## Review-size assessment

The 75-task implementation spans new shared contracts, a migration, API chat storage and routes, browser chat, coding-run integration, a separate PDF/image worker, bare Studio VLM lifecycle, deployment, and observability. Its non-generated source change is substantially larger than CONTRIBUTING.md's roughly 800-line PR threshold. Accordingly, 014 is the umbrella acceptance spec; implementation must be split into separately specced, reviewable child changes before code is written. Each child PR keeps the relevant constitution check, contract tests, runbook and operational gates. Child boundaries are:

1. `014a` shared chat and multimodal contracts with additive backward-compatible fields.
2. `014b` exact runtime dependency pins and architecture boundaries.
3. `014c` additive compatible multimodal, backend and run-activity contracts.
4. `014d` private chat persistence, typed failures and bounded settings.
5. `014e` verified chat authorization and shared model eligibility.
6. `014f` shared gateway stream execution and compatible regression protection.
7. `014g` native Chat picker, private conversation creation and initial telemetry.
8. `014h` persistent text turns and remaining shared inference admission.
9. `014i` browser event transport.
10. `014j` first-use Chat UI; cold-model experience remains a separate follow-up.
11. `014k` populated PostgreSQL migration proof and Alembic cleanup.
12. `014l` measured cold wait and failure remedy.
13. `014m` private history list/detail reads.
14. `014n` browser history navigation and draft separation.
15. `014o` same-tab text/model draft recovery.
16. Later children: queue status, history mutation, recovery, cancellation, coding actions, private file processing, bare Studio VLM, reasoning, final telemetry and end-to-end acceptance.

## Runtime dependency gate (014b)

- `uv lock --check`: pass; worker tree is `coire-core`, FastAPI/Uvicorn, Pillow 12.3.0 and pypdfium2 5.13.0, with no model engine. Linux `coire-node` tree omits MLX; macOS includes `mlx-lm` 0.31.3 and `mlx-vlm` 0.7.3.
- `react-markdown` 10.1.0 resolved in both npm and pnpm graphs. `pnpm` install reported its existing supply-chain policy pass. `npm audit --omit=dev` found zero production vulnerabilities; the full npm audit reported four dev-only findings (two moderate, two high) that require separate assessment before the final image gate.
- Web test/lint/build: 19 passed, lint and build passed. The runtime declarations do not yet implement the parser, visual serving, or Markdown rendering.

## Compatible contracts gate (014c)

- The node's published `specs/001-model-registry-node-agent/contracts/node-api.yaml` gained additive backend/vision fields so its strict EngineStatus response validator remains current. The failover page renders only text, consistent with its text-only tier.
- Core and gateway contract tests passed in the combined run; the node suite initially exposed two stale-contract failures, then all 19 node engine contract tests passed after updating the node YAML. Strict mypy passed for 215 source files and Ruff passed. OpenAPI and TypeScript API types were regenerated; OpenAPI freshness and web build/test/lint passed. Runtime VLM behavior and visual processing remain gated by later children.
- `coire-core/errors.py` referenced by the 014 plan and AGENTS.md did not exist at the base revision; existing API routes predominantly use FastAPI `HTTPException`. The typed chat failure and settings portion of parent T007 was completed in 014d. Route mapping remains a later API task.

## Persistence gate (014d)

- Added a single `0015_chat_conversations` migration after `0014_mcp_calls`, with seven private chat tables, model/variant `mlx_lm` server defaults, nullable visual measurements, owner constraints, ULID processing IDs and one-active-turn partial uniqueness. The preexisting text model survived upgrade/downgrade/re-upgrade with `backend=mlx_lm` and no visual capability.
- Ran upgrade and downgrade on disposable PostgreSQL 17 in the local `orbstack` Docker context. Populated chat blocked downgrade before any table drop; after deleting its rows, downgrade succeeded. SQL checks confirmed two active turns conflict, cross-owner attachment insertion fails, and invalid processing job IDs fail. Alembic metadata comparison found zero chat/backend differences. The disposable container was stopped and removed.
- Full pytest completed with 817 passed, 116 preexisting conditional skips and 256 warnings; strict mypy passed for 218 source files; Ruff and web typecheck/build and OpenAPI freshness passed. The automated migration test covers metadata and the downgrade guard. Parent T008 remains open until an automated populated upgrade/downgrade test is added and run.

The 014 tasks remain the full acceptance ledger. A child passing its local tests does not complete 014 or check later acceptance tasks. The `ChatFileProcessingRow` job ID was corrected to a ULID in `data-model.md` to follow the repository job-ID convention.

## Authorization and eligibility gate (014e)

- Native Chat requires a verified user-bound Access principal or user-owned API key with `chat` scope. Browser writes require an exact configured Origin; the empty default refuses them. Owner lookup returns the same 404 for missing, foreign and deleted rows, including admin callers.
- Existing nonadmin registry listings and compatible gateway resolution now use identity entitlements rather than API scopes. Native Chat has a stricter published/ready/entitled predicate even for admins; existing admin discovery behavior outside Chat is retained.
- The new guard and eligibility tests passed; the API/core suite passed 544 tests. The full suite passed 830 tests with 116 pre-existing conditional skips; Ruff and strict mypy passed (216 source files), as did OpenAPI freshness, web build and 19 web tests. Parent T009 remains open until actual list/send route contracts exist; T010 is complete.

## Shared gateway stream gate (014f)

- Moved streaming usage tracking, credential rechecks, cancellation-resistant finalization and public model rewriting into `gateway/execution.py`; `/v1` imports the same functions. The existing proxy still owns engine slots and leases.
- Direct tests cover fragmented CRLF SSE usage, public model rewriting, cancellation and first-token metrics; the existing gateway auth test covers credential revocation. Gateway auth and compatible contract tests passed 16/16. The full suite passed 834 tests with 116 pre-existing conditional skips, as did Ruff, strict mypy (217 source files), OpenAPI freshness and web build. Parent T011/T012 remain open because shared load/admission and the canonical native text adapter are not yet built.

## Native picker/create gate (014g)

- Added default-off `/api/v1/chat/models` and `/conversations` routes. The strict Chat eligibility predicate applies to admins too; owner, revision and IDs are server-derived. Selected missing/ineligible models share a safe 404. Visual picker support requires measured, verified VLM capability. Exact browser Origin is enforced by the shared guard.
- Added `COIRE_CHAT_ENABLED` and `COIRE_CHAT_PUBLIC_ORIGIN` compose settings, a CoireError problem mapper, spans/counter/content-free logs, provisioned dashboard/alert and an operations runbook. The default-off gate keeps partial 014 endpoints unavailable in a normal deployment.
- Five new picker/create contract tests passed. The full Python suite passed 839 tests with 116 pre-existing conditional skips; Ruff, strict mypy (221 source files), OpenAPI freshness, web test/lint/build and `docker compose config --quiet` passed. Dashboard JSON and alert YAML parsed and their image packaging references were checked. Parent T009/T015/T018 remain open until turn send and its contracts are present.

## Persistent text-turn gate (014h)

- Added locked owner/revision/idempotent admission, full saved-history context preflight, model snapshots and an output allowance bounded by the selected context. Duplicate sends follow saved events without a second engine request. New status and send routes use native Pydantic projections and SSE; generation uses the existing bare-engine gateway proxy, its slot/lease, and once-only usage accounting. Events and answer text commit before being emitted. Cold loads emit loading status and keepalives; failures and disconnects retain partial content with safe terminal state.
- A disposable local PostgreSQL 17 check caught two ORM flush-order FK violations that fake sessions missed. Inserting messages, then turn, then event before updating the conversation active-turn FK fixed them; a subsequent real DB check committed one turn plus status/delta/terminal events and confirmed the final answer and cleared active turn. The disposable container was removed.
- Streaming now closes the upstream generator on disconnect and rechecks live user, API-key and entitlement state during generation. Duplicate follower streams recheck user/key state. Unexpected errors log IDs and exception class only, without exception text or SQL parameters. The existing dashboard/alert covers send terminal failures, and the runbook notes the default-off release gate and later recovery work.
- Focused new tests cover owner/cross-parent refusals, version/active/idempotency conflicts, model switches, small/overflow context, cold loading, reported usage, persisted-before-emitted deltas, replay, revocation, failures, disconnect and privacy. The full Python suite passed 855 tests with 116 pre-existing conditional skips; Ruff, strict mypy (223 source files), OpenAPI freshness, web build/test/lint and Compose config passed. The later focused admission tests also passed. Parent T009/T015/T017/T018 are complete; tiny-model integration and remaining 014 features are open.

## Browser transport gate (014i)

- Exported the native SSE envelope as a `text/event-stream` OpenAPI response and regenerated browser types. Typed picker/create/status/send functions reside in the web API module. The send function validates event identity, order and terminal completion, and never retries its POST.
- The incremental parser handles fragmented UTF-8, CRLF, multiline data, comments and bounded frames. Admin snapshots use the same parser and retain their reconnect cursor behavior. Native generation stays connected in hidden tabs and aborts on unmount.
- New transport and hook tests passed with 27 web tests total; web typecheck/build and lint passed. The Chat send route contract checks its OpenAPI SSE schema; all eight text-turn contracts, Ruff and OpenAPI freshness passed. Parent T019 is complete. GET observers, SPA, history and cold-model display remain open.

## First-use Chat UI gate (014j)

- Extracted a shared Chat/Admin shell. Ordinary users land on Chat; the Admin link is visible only to admins and server authorization remains authoritative. The picker renders only the API's eligible entries with task grouping, metadata and an honest unknown warm-up estimate.
- A new conversation is created on the first send. Accepted events add input and assistant messages with the model's saved display-name snapshot; deltas grow the answer. Subsequent sends can switch models. Admission errors retain the unsent draft. An uncertain network failure reuses the same request identity on retry. The UI refuses overlapping sends while a conversation is being created.
- Markdown renders without raw HTML or automatic images and filters executable URLs. Web tests cover routing, empty picker, selection, streamed answer, model switch, draft preservation, retry identity and hostile Markdown. The web suite passed 35 tests; build/typecheck and lint passed. Parent T016 and T021 are complete. T020 stays open for full responsive/a11y/code-copy acceptance.

## Populated migration gate (014k)

- An opt-in automated test creates a unique database on an explicitly configured local PostgreSQL server, upgrades through 0014, seeds legacy model/user rows, upgrades to 0015, saves chat content, confirms downgrade refuses without removing content, clears chat, downgrades and verifies older model data survives, then re-upgrades. It ran successfully against a disposable local PostgreSQL 17 container; the container was removed.
- The first test run exposed an Alembic connection pool left open when downgrade raises. `alembic/env.py` now disposes the engine in `finally`; the rerun passed. The full Python suite passed 856 tests with 117 skips (the new opt-in database test plus pre-existing conditional integration/engine scenarios). Ruff and strict mypy passed. Parent T008 is complete.

## Measured cold wait gate (014l)

- Cold turns now persist the latest nonnegative measured engine load time as a nullable `turn.status` estimate. The composer shows the measured seconds or says the estimate is unavailable. Existing keepalives hold the connection; readiness resumes generation automatically. A load failure produces a content-free terminal suggestion to retry or choose another model.
- Focused backend streaming tests passed 9 cases, and web tests covered known/unknown picker estimates, inline wait and load failure. The web suite passed 38 tests; build/typecheck, lint, strict mypy and OpenAPI freshness passed. Parent T023–T026 remain open for actual queue-state integration and the full cold/eviction acceptance matrix.

## Private history read gate (014m)

- Added owner-scoped, tombstone-filtered newest-first conversation pages with bounded opaque timestamp/ID cursors. Detail uses a shared PostgreSQL conversation lock while reading a bounded message/related-turn page, saved partial answer/model names and event cursor. Browser API functions use generated page/detail types. Storage keys and model paths stay out of responses.
- Four focused route/service contracts passed for pagination, lock/owner predicates, partial output, safe 404s and JSON shape. The full Python suite passed 862 tests with 117 skips; strict mypy, Ruff, OpenAPI freshness, 39 web tests, build/typecheck and lint passed. Parent T027–T028 remain open for mutation, deletion, replay, Stop and recovery. Attachment summaries are supplied by the later file child.

## Browser history navigation gate (014n)

- Chat now lists saved conversations, opens owner-scoped message pages after reload, loads older list/message pages, and shows saved partial output with the original model-name snapshot. Narrow screens use an accessible history drawer. Selection generations discard stale async responses; separate in-memory drafts survive navigation between a saved conversation and a new one. A recorded active turn disables Send and is never automatically restarted.
- Browser tests cover drawer controls, reload/open, older pages, model attribution, stale selection response and draft separation. All 44 web tests, typecheck/build and lint passed. Parent T031–T034 remain open for delete, cross-tab reconciliation, session-scoped draft restoration and Stop/retry.
- With the server's default-off release flag, Chat now shows an explicit unavailable state instead of an empty picker or raw 404. The follow-up browser test brings the web suite to 45 passing tests; build/typecheck and lint remain green.

## Same-tab draft gate (014o)

- The verified user ID now keys a bounded sessionStorage draft map. Text and eligible model choice survive reload/re-authentication in the same tab; owner changes remove the previous owner's key. Accepted sends clear their draft and failed admissions retain it. Only text and model IDs are stored; no credentials, file bytes, response content or server paths.
- Storage helper and page tests cover UTF-8 byte bounds, invalid IDs, restore, owner change and send outcomes. The web suite passed 50 tests with build/typecheck and lint green. Parent T032–T033 remain open for explicit logout handling and future file selections.

## Owner Stop gate (014p)

- Added an owner-scoped, idempotent Stop route with a persisted `stop_requested` event. The controlling API process polls the durable turn state while waiting for cold load or engine output, then closes its upstream stream, releases its slot/lease, saves a `stopped` terminal and preserves partial answer text. A shared cold load survives one stopped waiter. Duplicate observers have no cancellation authority.
- Focused Stop, streaming and load-coordinator tests passed 17 cases. Strict mypy, Ruff, OpenAPI freshness and the 50 web tests/build/lint passed. Full Python suite was run for this child. Parent T029 remains open for expired-lease and crash reconciliation, and browser Stop wiring is a later child.

## Browser Stop gate (014q)

- An active plain-chat conversation now shows Stop, including after history load. The generated-type API call requests owner Stop and leaves the original SSE stream connected for its persisted terminal; partial output remains visible. A terminal response that wins the race refreshes saved conversation detail. Stop errors remain visible and retryable.
- A live-stream browser test covers the Stop POST, progress and saved partial output after the stopped terminal. Web test/build/lint gates passed. Parent T034 remains open for navigation and tab-close cancellation, observer reconciliation and explicit retry/continue.

## Plain Chat lease recovery gate (014r)

- Accepted text turns now receive a process-owned 30-second lease, renewed every five seconds by the live stream. Enabled API processes sweep stale turns on startup and bounded intervals. A locked pass commits one `interrupted` or `stopped` terminal, retains saved partial text and frees the conversation's active-turn slot without restarting generation.
- Unit tests cover expired running/stopping turns, repeated sweeps and fresh leases. The existing dashboard/alert show recovery outcomes and failed passes; the runbook describes inspection and rollback. Parent T029 remains open for real database/race acceptance and cross-tab recovery.

## Navigation Stop gate (014s)

- Leaving a conversation with a locally owned stream now persists `navigation` Stop before aborting transport. Viewing tabs can navigate without stopping another tab's generator. Late events from an aborted stream cannot replace the newly selected conversation. Disconnect cleanup checks the durable Stop before finalizing terminal state and usage.
- Browser and backend tests cover navigation Stop, draft separation and the disconnect race. Parent T034 remains open for observer reconciliation, explicit retry/continue and full tab-close acceptance.

## Read-only observer gate (014t)

- Added owner-scoped GET SSE replay over persisted conversation events, with scoped cursor validation and live user/key checks. A cursor gap caused by retention sends a replacement snapshot. The observer never opens a gateway stream or cancellation path.
- Owner/foreign/cursor contract and snapshot-gap unit tests passed; OpenAPI and generated browser types updated. Parent T014/T034 remain open for browser observer reconciliation and two-tab acceptance.

## Browser observer gate (014u)

- The shared SSE hook module now follows a selected conversation using scoped GET and bounded reconnects, pauses while the tab owns a POST stream, and stops on revoked/deleted access. Observer events refresh saved detail at a bounded cadence; cursor replacement snapshots update the selected view. Navigation generation checks discard stale reads.
- Browser tests cover scoped GET without POST and another tab's answer appearing within two seconds. Parent T034 remains open for full two-tab integration and explicit retry/continue.

## Versioned history edit gate (014v)

- Added locked owner/revision PATCH for title and eligible selected-model edits, with active-turn model conflict and persisted `conversation.updated` events. The browser history drawer exposes a keyboard-accessible rename form, retains rejected text and reloads revisions after conflict.
- Service, route and browser tests cover owner/foreign/stale/Origin and accepted/refused rename. Parent T027/T028/T031 remain open for deletion, purge, retry and broader version reconciliation.

## Immediate tombstone gate (014w)

- Added owner/revision DELETE with idempotent tombstone, active plain-chat Stop request and a content-free deletion event. Already connected observers receive the event and close; all new owner reads are denied. The browser requires explicit confirmation and removes selected state/draft after acceptance.
- Focused service/route/observer/browser tests cover owner/foreign/stale/Origin/idempotency and confirmation. Physical original/derived/content purge and alternate artifact-route guards remain in parent T030/T066; native Chat stays default-off.

## Deleted text purge gate (014x)

- Added reversible `0016_chat_purge_marker` and a bounded five-minute text-only purge pass. It removes saved messages/turns/events/quota rows, scrubs title/model selection and retains a content-free owner tombstone. Conversations with attachments or active turns remain unpurged for safe later cleanup.
- Unit tests cover safe, attached and active cases. A disposable local PostgreSQL 17 instance upgraded through 0016, held a populated deleted text conversation, and successfully purged its actual content rows while retaining a marked scrubbed tombstone. Downgrade to 0015 and re-upgrade succeeded. Parent T030/T066 remain open for attachment/blob/artifact purge and the deadline alert; Chat stays default-off.
- The existing populated 0015 migration integration test passed against the same disposable PostgreSQL instance, which was then removed. Full Python tests passed 885 with 117 conditional skips; 56 web tests, strict mypy (224 files), Ruff and OpenAPI freshness passed. Global `alembic check` still reports pre-existing metadata drift in unrelated tables; its diagnostics reported no chat-table difference at 0016.

## Event retention and overdue purge gate (014y)

- API maintenance now deletes expired SSE event rows in bounded 1000-row passes while leaving message history intact. It sets a content-free oldest-pending-purge age gauge; the Chat dashboard and `CoireChatPurgeOverdue` alert expose missed 24-hour cleanup deadlines, including attachments waiting on blob deletion.
- Unit tests cover bounded compaction and overdue age. Dashboard JSON/alert YAML parse checks passed. Parent T030/T066 remain open for staging, blob and coding-artifact cleanup; the release gate stays off.

## Bounded CPU file processor gate (014z)

- Added generated-key original reads with no-follow, 10 MiB and SHA-256 verification; bounded UTF-8, PDFium per-page Unicode extraction/selected scanned-page rasterization, and Pillow still-image orientation/normalization. Derived PNGs use temporary files and no-overwrite atomic publication under generated job/asset keys; result manifests use `coire-core` contracts. The parser performs deadline checks around operations. A future service process watchdog must enforce the hard native-call deadline.
- Nine runtime-generated fixture tests initially passed for text, malformed content, PNG metadata stripping and bounds, animation, scanned PDF rendering, digest, symlink and output collision. Two further expiry/collision tests were added; the full repository suite passed 898 tests with 117 conditional skips and 309 warnings. Strict mypy passed on 226 source files; Ruff check and format check passed. Two preexisting test-format deviations were corrected as part of the global format gate.
- Parent T046/T047 remain open for the authenticated serving worker, hard native-call deadline, encrypted PDF/crash recovery proof and integration; T048/T049/T066 and the final file/vision acceptance remain open. Native Chat stays default-off.

## Private worker service gate (014aa)

- Added strict authenticated health/process/status/cancel routes using core contracts, immutable per-process job requests, one active conversion, safe terminal results and cancellation suppression. Native parsing runs in a thread with a process-exit watchdog; the planned one-process Compose service is needed to make that boundary effective. Processing emits a span, low-cardinality outcome counter and content-free logs.
- Worker ASGI tests cover every route's bearer guard, strict path rejection, result manifest, idempotency, conflicts, busy refusal, cancellation, digest failure without automatic retry and watchdog arming/disarming. The worker suite passed 16 tests with 7 expected environmental/parser warnings. The full repository suite passed 903 tests with 117 conditional skips and 315 warnings; strict mypy passed on 228 source files, Ruff check/format and `uv lock --check` passed.
- Parent T046 is complete. T047 remains open for encrypted PDF, actual native hang/process recovery and integration; T048/T049/T066 remain open for hardened deployment, durable dispatch, publication and cleanup. Chat remains default-off.

## Opt-in worker deployment gate (014ab)

- Added a pinned distroless arm64 image containing only the worker/core dependency tree, one Uvicorn process and a credentialed loopback health probe. The `chat-files` Compose profile adds a private scheduler-worker processing network, read-only original/writable derivative volumes, 512 MiB/1 CPU limits and a dedicated Keychain-sourced token. Profile preflight refuses a missing token. Default Chat admission remains off and the worker profile is opt-in until durable dispatch, upload/publication and blob purge exist.
- Local OrbStack arm64 build succeeded. `scripts/image-policy.sh` passed all seven rules plus no harness/MLX and native PDFium/Pillow import. A disposable named volume accepted a non-root derived write; a disposable no-network, read-only-root container passed its credentialed health probe. Trivy CRITICAL scan exited 0 with no findings; Syft generated SPDX 2.3 with 35 packages at `/tmp/coire-file-worker-014ab.spdx.json` (local evidence, not committed). The disposable volume/container/secret file were removed.
- Compose default and `chat-files` profile parsed; topology/credential/worker focused tests passed 69 tests with one pre-existing conditional skip. `scripts/pin-images.sh --check` passed. Full Python suite passed 906 tests with 117 conditional skips and 315 warnings; strict mypy passed on 229 source files, Ruff check/format and `uv lock --check` passed. Parent T048 is complete. T049/T066 remain open for API/scheduler paths and verified content cleanup; Chat remains default-off.

## File admission contracts gate (014ac)

- Added strict shared upload/delete/process metadata and reused safe-basename validation. Worker requests now reject duplicate generated output IDs and page selections on inspect. Core contract tests cover unsafe filenames, unknown metadata, revisions and page/output bounds.
- The combined working tree after the following admission slice passed 914 Python tests with 117 conditional skips, 56 web tests/build/lint, Ruff, lock and OpenAPI freshness. Strict mypy passed for 230 source files and the new file contract tests. Running the CI-wide `mypy apps/ packages/` target exposes 119 existing errors in 14 older test files; those remain a final lint gate and were not introduced by these new modules. Native Chat remains default-off.

## Private original admission gate (014ad)

- Added bounded generated-key original staging, atomic no-overwrite publication, owner-row/locked conversation quota reservation, queued immutable processing job, attachment event and compensating file cleanup on DB failure. Owner/parent-bound metadata and original download routes refuse missing/deleted/cross-parent IDs; downloads verify the stored digest and refuse symlinks. The conversation detail projection includes current attachment summaries. The API image seeds non-root ownership for its private original volume; the worker sees that volume read-only. Nginx allows an 11 MiB multipart envelope only for the upload route.
- A disposable local PostgreSQL 17 container upgraded through 0016 and committed a real attachment, reservation, queued job and event with correct FKs/revision; it was stopped and removed. Focused contracts cover owner/Origin, revision/quota, staging limits, atomic rollback, safe headers and symlink refusal. The combined full suite passed 914 Python tests (117 conditional skips) and 56 web tests; Ruff, OpenAPI freshness, lock, web lint/build and strict mypy on 230 source files passed. The newer symlink and cancellation cleanup changes passed focused tests. `mypy apps/ packages/` still has 119 older test typing errors as recorded above.
- Local arm64 API/web images built and passed image policy; nginx syntax passed; Trivy CRITICAL scans exited 0 and Syft generated SPDX 2.3 artifacts. The final API image was rebuilt after the cleanup changes, rescanned and produced an 85-package SPDX SBOM. A disposable named original volume accepted a non-root API write. Parent T045/T049/T067 remain open for worker result publication, derived quotas/previews, API request-body and deletion races, full nginx acceptance and durable scheduler dispatch. Chat remains default-off.

## Typed worker client gate (014ae)

- The private typed HTTP client sends only configured internal worker calls with the dedicated bearer secret; validates strict response models/job identity; and maps busy, missing, network and malformed responses to content-free categories. It does not preserve raw worker response bodies as exception causes. One reserved generated output ID is now valid for inspect: a still image writes it, text/PDF ignore it. Pillow's early decompression-bomb refusal maps to `image_too_large`.
- Focused client/core/parser tests passed 22 cases. The full Python suite passed 920 with 117 conditional skips and 326 warnings; Ruff check/format, strict mypy on 231 source files and OpenAPI freshness passed. Parent T049 remains open for DBOS dispatch and result publication; default-off Chat protects the unfinished path.

## Durable file dispatch gate (014af)

- The scheduler now starts one queued/running file job at a time through DBOS. It commits an immutable generated-key worker request and deadline before the first POST. Recovery of a running row performs status GET only; a lost worker status fails visibly instead of parsing the original again. Known 429 refusals may retry within the original deadline.
- The workflow validates source ownership and result IDs, digest, type, page selection and reserved output IDs before persisting a processed manifest. Attachment/conversation tombstones cancel or discard late work. Spans, outcome counts and logs carry only identifiers and safe statuses. API byte verification and ready publication remain in parent T049; Chat remains default-off.
- Nine focused dispatch tests passed, including commit-before-POST, recovery, refusal, ambiguous submit/missing status, manifest mismatch, source mismatch and deletion. The full Python suite passed 928 with 117 conditional skips; Ruff check/format and strict mypy for the changed source/tests passed. The pre-existing CI-wide mypy test errors remain as recorded above.

## Verified file publication gate (014ag)

- API maintenance now consumes processed worker jobs in bounded passes. It rechecks immutable owner/source/request/result identities, reads generated PNG keys through no-follow descriptors from a read-only derived mount, verifies exact size/digest/header/dimensions, then commits ready attachment metadata, actual quota usage and a scoped attachment event together. Missing or altered output fails safely and retains its reservation until cleanup; deleted parents never publish.
- The result validator moved to a shared API module for scheduler/API use without pulling DBOS into the API. Nine focused publication tests cover text, image and PDF metadata success; altered/missing/symlinked/dimension-mismatched assets; owner mismatch, tombstone and idempotency. The full repository suite passed 936 Python tests with 117 conditional skips before two additional focused tests; all 18 dispatch/publication tests then passed. Ruff and strict mypy passed on changed files, Compose config parsed, and the local arm64 API image built. Image policy passed all rules and a Trivy CRITICAL scan exited 0 with no findings.
- Parent T049 remains open for owner retry/render/temporary processing; T066 remains open for original/derived purge. Native Chat remains default-off.

## Private preview gate (014ah)

- Ready attachment responses now include strict generated PNG preview metadata. An owner/parent-scoped preview route resolves only a committed asset ID and rechecks bytes, digest and dimensions on every read. It serves inline PNG with `nosniff`, `no-store`, sandbox and same-origin headers; original PDFs stay attachment downloads. Missing, cross-parent, foreign, deleted and changed assets are denied.
- OpenAPI and generated web types were regenerated. Focused route/publication tests passed 16 cases. The full Python suite passed 939 tests with 117 conditional skips; 56 web tests, web lint/build, Ruff across 442 files, strict mypy on 163 relevant source files and OpenAPI freshness passed.
- Parent T049/T050 still require explicit retry, PDF page rendering, file UI and temporary jobs; T066 requires verified blob purge. Chat remains default-off.

## Deleted conversation file purge gate (014ai)

- The worker now has an authenticated, idempotent generated-job output DELETE route that refuses active work and succeeds after its in-memory ledger is lost. The scheduler fences deleted-parent jobs in `purging`, retries the worker until derived output is gone, then commits `purged` and drops the stored result manifest. API maintenance waits for those markers before unlinking generated originals and deleting quota/job/attachment rows; text purge can then finish. A bounded age sweep removes abandoned generated upload staging files.
- Worker/client/scheduler/API tests cover bearer guards, active refusal, restart/idempotency, retry before marker, original cleanup gating and staging age. The full Python suite passed 944 tests with 117 conditional skips. Ruff (443 files), strict mypy (166 relevant source files), OpenAPI freshness and `chat-files` Compose config passed. Local arm64 worker/API/scheduler images built, passed image policy and CRITICAL Trivy scans with no findings.
- Parent T066 remains open for coding artifacts and end-to-end deletion proof; T049/T050 remain open for file retry/render/UI. Chat remains default-off.

## Failed file output cleanup gate (014aj)

- The scheduler now scans terminal failed file jobs whose JSONB manifest lacks `output_purged`. It asks the authenticated worker to delete generated outputs and records the marker only after success, preserving the visible failure code and attempt number. Busy/unavailable worker cleanup is retried; a future owner retry can require the marker before making another attempt.
- Focused tests cover busy refusal, retry, marker persistence and idempotency. The PostgreSQL JSONB filter compiles to a null-safe boolean condition. Full Python tests passed 945 with 117 conditional skips; Ruff on 443 files, strict mypy on 164 source files and OpenAPI freshness passed. Chat remains default-off pending the owner retry route and remaining parent tasks.

## Explicit file retry gate (014ak)

- Added an owner/parent/revision and same-origin protected POST for one explicit second `inspect` attempt. It requires a failed attempt-one job with its `output_purged` marker, binds the unchanged original key/digest to a fresh ULID job, reuses the existing quota reservation, moves the attachment to processing and emits a scoped event. The API never directly invokes the worker. A repeated request ID with its original revision returns the admitted state; a changed revision or third attempt is refused. Scheduler dispatch and failed-output cleanup preserve the client revision binding in the immutable manifest.
- Contract and dispatch/cleanup tests cover cleanup pending, stale revision, duplicate and altered replay, attempt cap, Origin and owner/parent guards. OpenAPI/browser types regenerated. Full Python tests passed 949 with 117 conditional skips; 56 web tests/lint/build, Ruff on 444 files, strict mypy on 166 relevant source files and OpenAPI freshness passed.
- Parent T049 remains open for explicit PDF page rendering and temporary visual jobs, T050 for file UI. Native Chat remains default-off.

## Explicit PDF page image gate (014al)

- The owner process route now queues only named pages from a ready PDF. It checks published page count and owner/conversation quota under locks, reserves up to 32 MiB of derived output and binds request ID, revision and page selection through dispatch. A failed render can be retried once after worker output cleanup with the unchanged selection. A completed render publishes verified page PNGs and keeps prior page-attributed Unicode extraction in `asset_manifest.text_result`.
- Contract tests cover out-of-range pages, quota refusal, duplicate request, selection changes and attempt two. API publication tests cover retained text, verified page metadata and actual quota shrink. The full Python suite passed 951 tests with 117 conditional skips; Ruff on 444 files, strict mypy on 165 relevant source files and OpenAPI freshness passed.
- Parent T049 remains open for temporary visual jobs and wider file action integration; T050 for file UI; T057 for visual gateway/context. Native Chat remains default-off.

## Failed file quota reclaim gate (014am)

- API maintenance now scans failed jobs with worker-confirmed `output_purged` and an active reservation exceeding original plus already published derived bytes. Under owner/conversation/attachment/job/reservation locks it releases the unused capacity; deleted conversations proceed through purge instead. Explicit inspect/render retries expand the same reservation under the two quota checks, so capacity used meanwhile causes a clear refusal without changing the failed file. Publication and reclaim now use the same attachment-before-job lock order as retry admission.
- Focused tests cover marker gating, idempotent release, tombstone exclusion, inspect retry re-reservation and quota refusal; the existing render quota contract still passes. Full Python tests passed 952 with 117 conditional skips. Ruff on 445 files, strict mypy on 166 relevant source files and OpenAPI freshness passed. Native Chat remains default-off pending the remaining file UI, coding and visual gates.

## Browser file preparation gate (014an)

- Added typed same-origin multipart upload, metadata/process helpers and owner-scoped original/preview URLs. The Chat hook creates a conversation before first upload, refreshes its revision and attachment list, polls processing metadata, and exposes explicit retry/render operations. The file panel shows state/error, original download, private PNG previews and selected PDF pages. A selected file blocks Send with a visible explanation until the turn/context path handles it, so no file is silently omitted.
- Browser client, component and Chat tests cover multipart admission, private preview paths, explicit page choices, failed retry and the selected-file send guard. Web tests, lint and build passed. Parent T050 remains open for turn attachment use, same-tab file draft restoration, model compatibility and complete page/text inclusion evidence. Native Chat remains default-off.

## Text file context gate (014ao)

- Native Chat admission now resolves ready, owner/conversation-bound text/code and PDF Unicode from the API-published manifest, checks result/source identity, refuses empty scanned-PDF text and visual modes, and preflights the exact prompt plus history. Migration 0017 stores an exact bounded prompt snapshot and selected file modes per user message, preserving the user's displayed text and later-turn context. The browser sends only ready text selections and shows saved file/mode attribution; visual and unready selections stay blocked.
- Generated OpenAPI and browser types were refreshed. A disposable local PostgreSQL 17 database upgraded through 0017, stored a populated prompt/selection message, downgraded to 0016 retaining visible text, then re-upgraded with the additive defaults; the container was removed. Full Python suite: 956 passed, 117 conditional skipped. Strict mypy passed on 159 API/core source files, changed-file Ruff, OpenAPI freshness, 62 web tests, web lint and build passed. Parent T049/T050/T057 remain open for visual context, temporary visual jobs, draft file persistence and end-to-end model compatibility; Chat remains default-off.

## Same-tab file draft gate (014ap)

- Session draft storage now validates and keeps only text/model IDs, file IDs, modes and page numbers. It rejects malformed/duplicate/over-limit selections, strips arbitrary fields before serialization, and clears the prior owner's store on identity change. The Chat hook saves selections as they change and restores them only after owner-scoped conversation detail confirms matching files and page counts. A ten-file UI bound prevents an oversized turn selection; visual selections still block Send.
- Storage and Chat tests cover malformed choices, identity clearing and PDF page restoration. All 65 web tests, lint and build passed. Parent T033 is complete; T032 and T050 remain open for broader session/logout and full visual file acceptance. Chat remains default-off.

## PDF parser hardening gate (014aq)

- The CPU worker classifies PDFium's typed password error as `pdf_password_required`, distinct from corrupt-PDF refusal; Chat renders a clear password-protected explanation. Small upstream PDFium fixtures with the redistribution license prove page-attributed Unicode extraction and encrypted refusal. A subprocess test proves a stuck native call exits the worker with status 124; existing scan, malformed, page bound, image bomb, metadata stripping and atomic output tests still pass. Parent T047 is complete.
- Full Python suite passed 959 with 117 conditional skips. Changed worker source passed strict mypy and Ruff; 65 web tests, lint and build passed. The local arm64 worker image built, passed all image policy rules including native PDFium/Pillow import, and Trivy CRITICAL scan exited 0 with no findings. Syft wrote an SPDX JSON SBOM to `/tmp/coire-file-worker-014aq.spdx.json` (local evidence). Native Chat remains default-off pending the remaining file, visual, coding and final acceptance gates.

## Chat reauthentication gate (014ar)

- Chat now classifies 401 responses from owner operations and its GET observer, displays a same-origin sign-in action and keeps the current same-tab draft. The observer stops after a 401 without replaying the original POST. Initial `/me` 401 shows the same action. Returning owner verification continues to determine whether drafts restore or the prior owner's store clears.
- Browser tests cover initial identity, failed send with retained draft, and one-shot observer expiry. All 68 web tests, lint and build passed. Parent T032 stays open for explicit logout and broader expiry/navigation acceptance; Chat remains default-off.

## Explicit retry gate (014as)

- A new retry may target only the latest failed/stopped/interrupted plain-Chat turn with unchanged original input and file choices. The API reauthorizes model/files, preflights context, reuses the input message ID and persists a new assistant attempt linked by `retry_of`. Future prompt history selects the latest assistant attempt per input; earlier partial attempts remain visible. The browser offers a separate Retry action, retains an unrelated unsent draft and appends only the new assistant bubble.
- Core/OpenAPI/browser types refreshed. Focused API contracts cover duplicate-input prevention, changed/non-latest refusal and later prompt selection; browser tests cover explicit retry and partial/draft retention. Full Python suite passed 962 with 117 conditional skips; 69 web tests, lint/build, strict mypy on 159 API/core source files, Ruff and OpenAPI freshness passed. The local arm64 API image built, passed policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014as.spdx.json` (local evidence). Parent T028/T034 remain open for explicit continuation and broader recovery acceptance. Chat remains default-off.

## Explicit continuation gate (014at)

- An owner can continue the latest failed, stopped or interrupted plain-Chat response only when it has saved nonblank partial text. The fixed continuation instruction creates a separate user and assistant turn linked to the prior turn, includes the partial in model history, and keeps an unrelated editor draft. Retry remains separate and keeps its original-input behavior. Request-ID replay still follows one admitted turn.
- Core/OpenAPI/browser types and migration 0018 add the recovery mode. Contracts refuse empty partials and altered instructions; browser tests check distinct messages, partial and draft retention. Full Python suite passed 965 with 117 conditional skips; 70 web tests, lint/build, strict mypy on 159 API/core source files, Ruff and OpenAPI freshness passed. Disposable local PostgreSQL 17 upgraded, downgraded to 0017 and re-upgraded, confirming the column; the container was removed. The local arm64 API image passed policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014at.spdx.json` (local evidence). Parent T028/T034 remain open for history/edit and broader browser recovery acceptance. Chat remains default-off.

## Reasoning separation gate (014au)

- The admitted model's capability profile selects a bounded incremental parser. Split `<think>` markers and unfinished reasoning remain outside the answer channel; an eligible engine's `reasoning_content` is assigned to reasoning. Each channel delta is persisted before SSE emission. Browser messages show saved/streamed reasoning in a closed native disclosure with inert text; the public answer remains separate. Parent T062 is complete; T061/T063/T064 retain broader cancellation, hostile Markdown and model-switch acceptance.
- Parser and streamed-channel tests passed. Full Python suite: 969 passed, 117 conditional skipped; 72 web tests, lint/build, strict mypy on 159 API/core source files, changed-file Ruff and OpenAPI freshness passed. Local arm64 API image passed policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014au.spdx.json` (local evidence). Chat remains default-off.

## Safe Markdown and code copy gate (014av)

- Fenced code now has a plain-text **Copy code** control with a visible failure state. Protocol-relative links join executable links as inert text; raw HTML and remote images remain inert. Browser tests cover the rendered DOM, successful clipboard text and refusal. Parent T063/T064 remain open for attachment-model compatibility and broader visual remedies.
- All 74 web tests, lint and build passed. The local arm64 web image built, passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-web-014av.spdx.json` (local evidence). Chat remains default-off.

## Reasoning boundary gate (014aw)

- Parser tests now cover mixed answer/reasoning content; native stream tests cover a direct `reasoning_content` field and owner Stop while a split opening delimiter is held. Stop emits no answer delta for that fragment. Parent T061 is complete. Focused parser/stream suite passed 19 tests; Ruff passed. This test-only slice does not change an image. Chat remains default-off.

## Observed load status gate (014ax)

- A cold Chat turn now observes the latest active placement instance. Requested/reserving publishes `queued`; launching/warming publishes `loading`; `running` follows engine readiness. Each phase change uses the existing persisted owner event. The historical warm-up estimate stays nullable and no rank or percentage is inferred. Parent T025 is complete; T026 still needs browser cold/queue/failure interaction acceptance and the <=1-second status measurement.
- Focused status mapping and cold-stream tests passed. Full Python suite: 978 passed, 117 conditional skipped. Strict mypy on 159 API/core source files, changed-file Ruff and OpenAPI freshness passed. Local arm64 API image passed policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014ax.spdx.json` (local evidence). Chat remains default-off.

## Bare vision node lifecycle gate (014ay)

- The node now has separate fixed bare `mlx_vlm.server` argv from its verified local store path, offline/no-Hub-token/no-remote-code environment, bounded default vision cache and one concurrent sequence. Backend identity survives node process state, re-adoption and orphan discovery; a mismatched duplicate load is refused. The registry backend travels through admin, gateway and placement load callers; migration 0019 persists it in control-plane engine rows with a text default. Existing generation-based readiness and memory-estimate admission remain in force. Mocked tests did not start Metal or weights. Parent T052/T053/T056 remain open for visual validation, memory/placement compatibility and tiny-model behavior.
- Full Python suite: 984 passed, 117 conditional skipped. Strict mypy on 209 node/API/core source files, changed-file Ruff and OpenAPI freshness passed. Disposable local PostgreSQL 17 upgraded through 0019, downgraded to 0018 and re-upgraded, confirming the text default; container removed. Local arm64 API and scheduler images built, passed policy and Trivy CRITICAL scans with no findings; Syft wrote `/tmp/coire-api-014ay-final.spdx.json` and `/tmp/coire-scheduler-014ay-final.spdx.json` (local evidence). Native Chat remains default-off.

## Compatible gateway visual guard (014az)

- OpenAI text parts now count actual text characters in context preflight. Inline image parts receive 400 before run spending or engine transport while the bounded temporary visual path is unfinished; core already refuses HTTP/file image URLs. Anthropic image and other unsupported blocks receive 400 before model resolution and cannot be silently dropped by the adapter. This is an interim refusal; parent T051/T057 stay open for verified VLM visual admission.
- Route and unit contracts passed. Full Python suite: 989 passed, 117 conditional skipped. Strict mypy on 160 API/core source files, changed-file Ruff and OpenAPI freshness passed. Local arm64 API image built, passed policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014az.spdx.json` (local evidence). Chat remains default-off.

## Composer status acceptance tests (014ba)

- Component tests verify that unknown warm-up, queue and failure status leave the draft intact and disable Send, then allow the same draft through Enter after recovery. Shift+Enter remains multiline. Parent T024 is complete; T026 still needs the browser/engine interaction timing acceptance. All 76 web tests, lint and build passed. This test-only slice changes no production image. Chat remains default-off.

## Locked native node install gate (014bb)

- The operator build script exports the node graph from `uv.lock`, selects 68 compatible CPython 3.13/macOS arm64 wheels, verifies locked SHA-256 and size, and stages them with the core/node wheels. `--local-only` completed without contacting a Studio. Generated `dist` artifacts are ignored. The installer consumes hashed requirements without an index into a new digest-named environment. Both bare server CLI imports/help run before atomic symlink activation; a failed smoke leaves the prior active link in place. Parent T060 is complete; the operator cluster rollout and tiny-model acceptance in T074/T075 remain open.
- A disposable local prefix installed the actual 68-wheel graph offline and activated `envs/0.2.0-14980059f8d9` after smoke, with `mlx-vlm 0.7.3`, `mlx-lm 0.31.3` and `coire-node 0.2.0`. No engine process, weights or Studio was touched. Four focused installer tests passed. Full Python suite: 993 passed, 117 conditional skipped. Strict mypy on 212 source files, changed-file Ruff and format, shell syntax and OpenAPI freshness passed. The correct OpenAPI command is `uv run python -m coire_api.openapi --check`; the older `coire-api` executable in AGENTS.md is unavailable. No production image changed. Chat remains default-off.

## Vision readiness generation gate (014bc)

- Inspection of the pinned `mlx-vlm==0.7.3` wheel showed its bare chat request requires `model`. The node readiness probe now supplies only the owned verified local store path for a VLM, allowing one-token generation to establish readiness. The text probe shape remains unchanged. A mocked lifecycle contract verifies health-to-generation-to-ready without Metal or weights. Parent T052/T053 stay open for remaining node validation, budget, cancellation and tiny-model cases.
- Full Python suite: 994 passed, 117 conditional skipped. Strict mypy on 50 node/test source files, changed-file Ruff and format, OpenAPI freshness and native node wheel build passed. No Studio or real engine was contacted. Chat remains default-off.

## Chat loading contract gate (014bd)

- Authenticated picker contracts now prove a selected model remains listed after its ready engine is evicted, the last measured warm-up remains visible, an unmeasured starting engine has a null estimate, and no rank/percent/ETA fields are invented. Existing native stream tests prove observed queue/loading transitions, safe load failure and Stop during cold load. Parent T023 is complete; T026 still needs browser timing and interaction acceptance.
- Focused loading suite: 12 passed, 11 unrelated cases deselected. Changed test file passed strict mypy, Ruff and format. This test-only slice changes no production image. Chat remains default-off.

## Chat stream contract audit (014be)

- Audited the existing shared SSE parser, same-origin native POST and cursor-bearing GET observer. Existing tests cover fragmented UTF-8, CRLF, comments, multiline frames, terminal/refused POSTs, one-shot send, auth failure, hidden generation and admin reconnect. Added an expired-cursor observer test: a 409 resets to the conversation's zero cursor, then a saved replacement snapshot is accepted through GET only. Parent T013/T014 are complete; browser end-to-end acceptance remains open.
- All 77 web tests, lint and TypeScript production build passed. This test-only slice changes no production image. Chat remains default-off.

## Safe public gateway stream rewrite (014bf)

- The compatible OpenAI SSE boundary now buffers a bounded complete event across fragmented transport chunks and CRLF/multiline data before rewriting `model`. It refuses malformed/incomplete/oversized frames and any output field containing the resolved node-local model path. Rewriting now runs inside usage tracking, so a bad engine frame records failed usage and closes upstream. Existing loader, proxy lease, route, stream accounting and cancellation regressions plus new fragmented/path tests cover parent T011; T012 remains open for full shared execution extraction.
- Full Python suite: 1000 passed, 117 conditional skipped. Strict mypy on 160 API/core source files, changed-file Ruff/format and OpenAPI freshness passed. The local arm64 API image built, passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014bf.spdx.json` (local evidence). Chat remains default-off.

## Explicit browser sign-out gate (014bg)

- The shared authenticated Chat/admin shell now offers **Sign out**, which clears the verified owner's same-tab Chat drafts before navigating to Cloudflare Access's documented same-origin logout path. Session expiry still preserves drafts for reauthentication, while identity changes clear the prior owner's store. Shell and storage tests cover the explicit action. The existing shell, routing, picker, composer, transcript and safe Markdown tests were audited and passed, completing parent T020 and T032. Browser device/keyboard acceptance remains T073.
- All 79 web tests, lint, TypeScript production build and changed-file Prettier passed. The local arm64 web image built, passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-web-014bg.spdx.json` (local evidence). Native Chat remains default-off.

## Chat artifact tombstone guard (014bh)

- Owner deletion now expires any branch artifact linked through a Chat coding turn before committing the tombstone. The preexisting MCP metadata and download lookup refuses a tombstoned Chat conversation as well, including before its artifact expiry update is observed. Ordinary MCP artifacts retain their owner and expiry checks. Parent T042 remains open for the Chat-owned artifact route and the coding adapter.
- Focused contracts passed 7 tests. Full Python suite: 1002 passed, 117 conditional skipped. Strict mypy on 160 API/core source files, changed-file Ruff/format and OpenAPI freshness passed. The local arm64 API image built, passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014bh.spdx.json` (local evidence). Chat remains default-off.

## Cold stream load-task cleanup (014bi)

- The shared gateway execution context cancels and drains a pending load when an OpenAI, Anthropic or native Chat cold stream closes during keepalive. A completed load continues through its existing proxy and usage path. Parent T012 remains open for canonical text adapter and full admission extraction.
- Full Python suite: 1004 passed, 117 conditional skipped; the subsequently added native-close test and 46 focused stream regressions also passed. Strict mypy on 161 API/core and focused execution test source files, changed-file Ruff/format and OpenAPI freshness passed. The local arm64 API image built, passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-api-014bi.spdx.json` (local evidence). Chat remains default-off.

## Parser, worker and VLM observability (014bj)

- Native Chat now counts malformed and missing-DONE engine streams once by fixed reason, with no prompt or identifier label. Existing node load and resident metrics include the fixed backend label; existing worker processing outcomes feed the new panel. The Chat dashboard now shows parser failures, file outcomes and bare VLM load p95; baseline parser and worker failure alerts join the existing API failure and overdue-purge alerts. Dashboard/alert wiring and malformed/missing-DONE counter tests passed. Parent T069 is complete; T068 remains open for the rest of the specified path telemetry.
- Focused parser/observability/node tests passed 8 cases. Full Python suite: 1008 passed, 117 conditional skipped. Strict mypy passed 209 API/core/node source files, changed-file Ruff/format and OpenAPI freshness passed; dashboard JSON and alert YAML parsed, and `promtool check rules` accepted all four Chat rules. Lean and diagnostics Compose configs passed; the Prometheus image includes alerts in both profiles, and diagnostics Grafana includes the dashboard. Native node wheel built. Local arm64 API, Prometheus and Grafana images built, passed image policy and Trivy CRITICAL scans with no findings; Syft wrote `/tmp/coire-api-014bj.spdx.json`, `/tmp/coire-prometheus-014bj.spdx.json` and `/tmp/coire-grafana-014bj.spdx.json` (local evidence). Chat remains default-off.

## Isolated coding activity writer (014bk)

- The coding harness now appends strict `RunActivity` receipts for actual context reading, model generation, edit application, tests and branch bundle work in its separate output mount. Each run uses a UUID-named, mode-0600, no-follow JSONL spool, with fixed tool names, fixed failure codes, sequence/byte limits and one overflow marker. The spool never receives arguments, file paths, prompt text or exception text. Agent tests prove lifecycle, failure privacy, byte/count caps, symlink refusal and actual Apply activity. The node reader, durable event collection and UI remain open in parent T040–T043; parent T038/T039 remain open until these are integrated.
- The default pytest discovery previously omitted the entire agent test directory. It now includes it, with the ops-only source path added for the two existing ops imports. All 44 agent tests pass; the full Python suite now passes 1052 with 117 conditional skips. Strict mypy passed 49 agent/core source files; changed-file Ruff/format passed. The local arm64 agent image built, passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-agent-014bk.spdx.json` (local evidence). Chat remains default-off.

## Authenticated Studio activity reader (014bl)

- The node now exposes a bearer-guarded `GET /node/runs/{run_id}/activity` page. It checks managed/run/node labels before fetching only the fixed per-run path from the separate output mount, caps archive and payload bytes, validates strict core records, contiguous sequence, allowlisted tool names and a terminal overflow marker, then returns up to 100 records after the caller's cursor. Missing output is typed unavailable; overflow is typed truncated. Foreign labels, malformed/extra/oversized archives and invalid cursors are refused. Workspace cleanup tests confirm the spool is erased with the run output. Parent T038–T040 are complete; durable Chat event collection T041 remains open.
- Focused node/agent/activity and cleanup tests passed 18 plus the later cleanup subset of 13. Full Python suite: 1064 passed, 117 conditional skipped. Strict mypy passed 98 core/node/agent source files; changed-file Ruff/format passed. Native node wheel built. The rebuilt local arm64 agent image passed image policy and Trivy CRITICAL scan with no findings; Syft wrote `/tmp/coire-agent-014bl.spdx.json` (local evidence). No Studio or engine was contacted. Chat remains default-off.

## Durable coding activity event bridge (014bm)

- Core contracts now carry allowlisted tools, paired UUID call IDs, a 2 MiB spool cap, and owner-visible `run.activity` plus final `run.activity_status` events. Migration 0020 adds the durable sequence, final state and unique assigned-run index. The API client validates node pages and the collector verifies run, coding-call and owner identity under a locked conversation, persisting each sequence once. WAIT polls every 0.5 s; completion drains before REMOVE; owner kill drains available receipts before KILL removes output. A restarted collector resumes from the stored sequence. Existing owner GET replay reconstructs both event types; plain MCP runs skip Chat collection. Parent T041 remains open until the Chat coding admission/result bridge in T037 is integrated.
- Full Python suite: 1071 passed, 118 conditional skipped. Later focused replay/restart cases passed 6. All 79 web tests, lint and build, strict mypy on 225 source files, Ruff/format and OpenAPI freshness passed. A disposable local PostgreSQL 17 seeded an old turn, upgraded to 0020, downgraded to 0019 and re-upgraded, retaining the turn; container removed. Native node package and 68 locked macOS arm64 wheels built locally. Local arm64 API, scheduler and agent images built, passed image policy and Trivy CRITICAL scans with no findings; Syft generated `/tmp/coire-api-014bm.spdx.json`, `/tmp/coire-scheduler-014bm.spdx.json` and `/tmp/coire-agent-014bm.spdx.json`. No Studio or engine was contacted. Chat remains default-off.

## Chat Apply artifact download (014bn)

- The Chat route now checks live conversation ownership, Apply turn, stored successful call, exact run and matching retained artifact before handing the request to the existing Studio status/digest/size-verified stream. The existing MCP URL retains its owner, expiry and Chat tombstone checks. Contract tests cover foreign/cross-parent/deleted/failed/mismatched references and authenticated route metadata. Parent T042 is complete; Chat coding admission/result delivery in T037/T041 remains open.
- Full Python suite: 1076 passed, 118 conditional skipped. All 79 web tests, lint and build, strict mypy on 226 source files, changed test mypy, Ruff/format and OpenAPI freshness passed. The local arm64 API image built, passed image policy and Trivy CRITICAL scan with zero findings; Syft generated `/tmp/coire-api-014bn.spdx.json` (local evidence). Chat remains default-off.

## Chat coding run bridge (014bo)

- Browser Chat code actions now bind an owner registered workspace and any prior Research/Plan result to the same source; Plan and Apply pin the resolved revision. Apply requires a prior plan and passes through the existing harness-verified write variant gate. The Chat messages, turn, coding call, run, accepted event and audit row commit together. Shared call creation and run kill serve MCP while its API-key scope gate remains. Controlling browser disconnect and owner Stop revoke the run token and kill the assigned Studio container; a queued, unplaced run stops without Studio I/O. The scheduler drains activity, then persists a typed Research/Plan/Apply result and terminal event before cleanup. The owner turn detail recovers the typed result after event retention. Parent T036/T037/T041 are complete; code-mode browser UI, visual coding and local model acceptance remain open.
- Focused source/bridge/Stop, MCP, schema and scheduler cases passed. Full Python suite: 1085 passed, 118 conditional skipped. All 79 web tests, lint and build, strict mypy on 227 source files plus new focused test modules, Ruff/format and OpenAPI freshness passed. Local arm64 API, scheduler and MCP images built, passed policy and Trivy CRITICAL scans with zero findings; Syft generated `/tmp/coire-api-014bo-complete.spdx.json`, `/tmp/coire-scheduler-014bo-complete.spdx.json` and `/tmp/coire-mcp-014bo-complete.spdx.json`. No Studio or engine was contacted. Chat remains default-off.
## Provider registration and picker source (014bw, 2026-09-29)

- A strict admin-only provider registration contract now accepts fixed OpenAI or Anthropic model identifiers, declared context/output limits and a daily token budget. It records the target in a reversible 0021 registry migration, starts private, writes a typed principal audit and a state transition, and rejects caller-supplied endpoints or credentials. Publication is blocked until provider routing and budget enforcement are implemented. Studio models keep their existing path; Code mode excludes provider targets.
- Native Chat picker and generated TypeScript contracts identify Studio, OpenAI or Anthropic sources. A published provider target projects remote readiness and has no Studio warm-up. The web picker labels the source. Parent T076 and child J001 are complete; T077–T080 remain open. Native Chat and visual flags remain default-off.
- Full Python suite passed 1,114 with 119 conditional skips; focused provider and picker contracts passed. Strict mypy, Ruff and OpenAPI freshness passed. Web test, lint and build were rerun after source-label changes.
