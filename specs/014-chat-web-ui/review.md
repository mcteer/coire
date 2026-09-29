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
9. `014i` chat SPA, event transport and cold-model experience.
   Browser event transport is a separate first slice; SPA and cold-model experience follow.
10. `014j` history, recovery, cancellation and coding actions.
11. `014k` private file processing worker, storage, quotas and previews.
12. `014l` bare Studio VLM acquisition, serving and visual gateway/harness inputs.
13. `014m` reasoning UI, final telemetry, dashboards and end-to-end acceptance.

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
