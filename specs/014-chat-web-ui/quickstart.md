# Acceptance guide: Chat Web UI

This is a future implementation validation guide. No feature tests or engine checks have been run during planning. Record real outcomes in `specs/014-chat-web-ui/review.md` during implementation; do not mark a task complete from this guide alone.

## Prerequisites

- Use `feat/014-chat-web-ui` based on current main, with implementation tasks, the single migration and the isolated file-worker image applied to a disposable local deployment.
- Follow the repository's local compose/integration setup. Use only a locally acquired/approved tiny model <=1 GB in `COIRE_TEST_MODEL`; record its ID, source and byte size. Do not download through Chat or run tests against real Studios from CI/agent tools.
- Prepare two users and an admin, published/ready loaded/cold models, entitlement pairs and verified/unverified coding variants. Add an admin-acquired/validated local tiny vision model <=1 GB via `COIRE_TEST_VISION_MODEL`; record exact version/bytes. Research identifies a roughly 150 MB SmolVLM candidate, not an already-passed acceptance. Fixture-only states may exercise negative cases, while real text and visual stream/cancel gates use actual local engines.
- Prepare an allowlisted sample Git repository with a passing test, a deterministic failing test and a branch/revision suitable for Research/Plan/Apply. Existing 013 helpers supply workspace setup and artifact verification.
- Configure exact `COIRE_CHAT_PUBLIC_ORIGIN`, original/derived volume ownership, worker Keychain-sourced secret and bounds from plan; use the locked node environment. Preserve all container/network policy. Never put credentials into evidence/fixtures.

## Static, contract and compatibility gates

Run after implementation, using the existing workspace toolchain:

```bash
uv sync --all-packages
uv run ruff format --check
uv run ruff check .
uv run mypy
uv run pytest -q -m 'not integration'
uv run python -m coire_api.openapi --check
pnpm -C apps/coire-web test
pnpm -C apps/coire-web lint
pnpm -C apps/coire-web exec tsc --noEmit
```

Regenerate changed contracts before the freshness check:

```bash
uv run python -m coire_api.openapi
pnpm -C apps/coire-web exec openapi-typescript ../coire-api/openapi.json -o src/api/schema.d.ts
```

Verify both web lockfiles and the Python/node dependency staging pins. Build/policy-check/scan/SBOM API/web/scheduler/agent/file-worker and other affected images; verify worker arm64 native libraries/notices and locked node-wheel staging. Validate compose config. In a disposable DB upgrade/downgrade/re-upgrade, including existing text defaults and visual rows; verify `/v1`, MCP/admin/failover compatibility. Never use a populated live DB for destructive reversal tests.

## Story acceptance

| Story | Actions | Required observation |
| --- | --- | --- |
| US1 — Chat and models | Ordinary user opens Chat, starts streaming chat, switches model, reloads; open picker as entitled/unentitled/admin user | Chat needs no admin data; published+ready+entitled entries only, no node/variant IDs; streaming answer and correct per-response model; preserved input on failure. |
| US2 — Cold start | Choose cold model with measured duration, then one without measurement; induce actual load failure and queue wait | Inline state appears <=1 s; measured estimate or explicit unknown; no fake progress/rank; automatically streams on ready; safe retry guidance on failure. |
| US3 — History and recovery | Two tabs send against same revision, repeat request ID, disconnect after tokens, restart local API, retry/continue, delete history | Exactly one accepted turn; second draft intact; observer <=2 s; partial text survives; no reconnect-driven regeneration; explicit attempt history; immediate access denial and purge within configured deadline. |
| US4 — Code | Research/Plan on unverified model; refuse Apply; use verified variant and matching plan; observe tools, stop mid-run, download bundle | Actual tool events while running; new clone at stated revision; write verification; <=5 s healthy kill and revoked credential; branch/diff/honest tests; owner bundle digest verifies and imports. Existing durable run survives an API restart as the same run. |
| US5 — Files and reasoning | Upload/reuse text/code/PDF/scanned PDF/images; select text/visual mode/pages; reject malformed/encrypted/animated/oversized files and quota; exceed text/visual context; stream split thinking tags/hostile Markdown | All content choices visible; scans/diagrams/images reach eligible vision model; incompatible model blocked; normalized private previews/original downloads; no truncation or reasoning leakage; limits precede inference and native parser failure cannot kill API. |

Run new local integration acceptance after its fixture is implemented:

```bash
COIRE_INTEGRATION=1 uv run pytest -q -m integration tests/integration/test_chat_ui.py
```

Cover actual tiny text stream → transcript → Stop, and actual tiny vision image/PDF-page question → visual usage/context → Stop with core model execution prohibited. Exercise VLM load/health/stop/re-adoption and offline failure on missing local processor/weights. Use runtime-generated image/PDF fixtures rather than committing generated assets. Test coding visual parts through sandbox/readonly input/relay/retries and the verified-write gate; use deterministic faults for race/overflow/cursor cases. Document any operator-only capable-model visual coding check explicitly. No 014 acceptance may pass through skips.

## Browser acceptance

Use actual Safari and Chromium at 1440×900 and 1024×768 on an Apple Silicon development Mac. Record versions/hardware and screenshots. Compare against `docs/design/mockups/chat.html` and tokens, with spec privacy/behavior overriding sample internal labels.

1. Complete chat/model switch/cold wait without assistance as an unfamiliar user; note time and any intervention.
2. Use keyboard alone for history/new conversation, model picker, file selection, send (`Cmd/Ctrl+Enter`), Stop, reasoning disclosure and drawer close (`Esc`). Check visible focus, focus return, icon labels, textual statuses and non-token-by-token live announcements.
3. Enable reduced motion. Verify composer/dock never overlap and side panels become usable drawers below 1200 px. Preserve scroll position while reading an older message.
4. Seed 200 messages and a 50,000-character response. While streaming, measure typing, Stop and picker-open interactions in browser performance tools, at least 30 samples per action; p95 <=100 ms. Record results, not just a screenshot.
5. Hide/show the tab: generation continues. Navigate away/close the tab: healthy generation stops. Expire the session and reauthenticate in the same tab: draft restored only for the same identity. Logout/change identity clears drafts.
6. Render script tags, `javascript:`/data links, Markdown remote images, incomplete fences and split UTF-8 frames. Assert no execution or external image request; code copy yields literal text. Private validated attachment raster previews remain functional.
7. Upload a searchable PDF, image-only scan, mixed pages/diagram and photo. Select explicit pages and text/visual mode, switch to a text-only model and confirm guidance. Try more than ten visual units or fifty PDF pages. No empty scan or silently omitted page can be sent as successful content.

## Privacy, operations and rollback

Attempt every conversation/original/preview/derived asset/event/result/artifact access as second owner/admin; expect uniform refusal. Test alternate MCP artifact access after deletion. Race original+derived quota reservations; test traversal, abandoned jobs and temporary `/v1` image cleanup. Kill/timeout the disposable local file worker during conversion; verify API survives, job fails, partial outputs are cleaned and retry is bounded. Exercise output/decompression/selected-page bounds with visible failures.

Inspect chat/file-worker/vision spans/metrics/logs: IDs/outcomes only, no content or image data. Fire chat, parser and purge alerts in lean mode; inspect diagnostic panel/history when enabled and confirm baseline continues when disabled. Measure overhead, stop latency and visual working-memory reservations; no request may push the node into swap.

Validate rollback on a disposable deployment: disable Chat/visual admission, stop turns/parser jobs and VLM engines, remove VLM registry entries from serveable state through audited admin lifecycle, then roll back images/node symlink while retaining chat schema/data. Verify prior text API/admin/MCP. Schema downgrade is separate, requiring explicit backup/export and acknowledged loss; uploaded originals/derivatives are not deleted by image rollback. Test both a broken VLM environment smoke rollback and an older node refusing visual placement.
