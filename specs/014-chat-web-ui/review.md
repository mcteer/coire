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
3. `014c` persistent text chat API, model eligibility and shared inference execution.
4. `014d` chat SPA, event transport and cold-model experience.
5. `014e` history, recovery, cancellation and coding actions.
6. `014f` private file processing worker, storage, quotas and previews.
7. `014g` bare Studio VLM acquisition, serving and visual gateway/harness inputs.
8. `014h` reasoning UI, final telemetry, dashboards and end-to-end acceptance.

## Runtime dependency gate (014b)

- `uv lock --check`: pass; worker tree is `coire-core`, FastAPI/Uvicorn, Pillow 12.3.0 and pypdfium2 5.13.0, with no model engine. Linux `coire-node` tree omits MLX; macOS includes `mlx-lm` 0.31.3 and `mlx-vlm` 0.7.3.
- `react-markdown` 10.1.0 resolved in both npm and pnpm graphs. `pnpm` install reported its existing supply-chain policy pass. `npm audit --omit=dev` found zero production vulnerabilities; the full npm audit reported four dev-only findings (two moderate, two high) that require separate assessment before the final image gate.
- Web test/lint/build: 19 passed, lint and build passed. The runtime declarations do not yet implement the parser, visual serving, or Markdown rendering.

The 014 tasks remain the full acceptance ledger. A child passing its local tests does not complete 014 or check later acceptance tasks. The `ChatFileProcessingRow` job ID was corrected to a ULID in `data-model.md` to follow the repository job-ID convention.
