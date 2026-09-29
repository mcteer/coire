# Feature 014 implementation review

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
- Parent T049 remains open for owner retry/render/temporary processing and private preview routes; T066 remains open for original/derived purge. Native Chat remains default-off.
