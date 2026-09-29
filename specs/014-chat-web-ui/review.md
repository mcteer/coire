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
15. Later children: queue status, history mutation, recovery, cancellation, coding actions, private file processing, bare Studio VLM, reasoning, final telemetry and end-to-end acceptance.

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
