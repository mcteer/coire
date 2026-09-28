# Feature 013 Validation

## Prerequisites

- Feature 013 images are built and pass image policy/CVE scan; `coire-mcp` optional profile is enabled.
- An MCP-scoped API key with a user identity, one published coding model, and one harness-verified coding variant exist.
- A tiny model (≤1 GB) is available for integration tests. Use a disposable sample Git repository on an allowed source host.
- Studio control path and node agent are healthy. Record any unavailable real-cluster gate; a skipped probe is not a pass.

## Contract and isolation checks

1. Run unit and contract tests, OpenAPI freshness, strict mypy, lint, and MCP client protocol tests. Enumerate tools and confirm exactly three names.
2. Call each tool without a credential and with a key lacking `mcp`; expect 401 and 403. Try chat/admin/image method names; expect no such tool.
3. Use an unverified published model for research and plan; expect read-only results. Use it for apply; expect a verification refusal before a run starts.
4. Attempt an unapproved URL, a local/private resolved address, path traversal, and two simultaneous applies to one source. Confirm refusal or isolated clones as applicable.
5. Disconnect during a long run, and separately use admin kill. Confirm the container stops and token is revoked within 5 seconds.

## End-to-end tiny-model loop

1. Connect an ordinary MCP client to `/mcp`; call research on a sample repository and verify cited path/line pairs against the checked-out revision.
2. Call plan using the research ID. Confirm ordered steps and acceptance criteria, and no file change after either read call.
3. Call apply with a verified model. Confirm a non-default branch, commit, bounded diff, test counts/status, and owner-downloadable branch artifact. Import the artifact into a separate clone and verify its head revision.
4. Repeat with deliberately failing tests. Confirm branch/diff/artifact remain available and test status is `failed`. Repeat with no tests and confirm `not_found`; use an unsupported test runtime and confirm `unsupported`, never `passed`.
5. Restart only `coire-mcp` during chat traffic and confirm chat continues. Inspect run listing, audit row, metric, span, dashboard panel, alert rule, and bounded cleanup.

Record commands, counts, CI links, and real-cluster evidence here during implementation. Do not mark acceptance complete on an unrun or skipped test.

## Local validation record (2026-09-27)

- `uv run ruff check .` and `uv run mypy apps packages`: passed (389 source files).
- `uv run pytest -q -m 'not integration'`: 790 passed, 8 skipped, 109 deselected.
- `uv run python -m coire_api.openapi --check`: passed.
- `pnpm -C apps/coire-web test`, `lint`, and `exec tsc --noEmit`: passed (19 web tests).
- `uv run alembic -c apps/coire-api/alembic.ini heads`: one head, `0014_mcp_calls`.
- Local arm64 agent and MCP images: build and image-policy passed. Trivy CRITICAL scans passed. The agent image ran Git and produced a branch bundle.
- `uv run pytest --collect-only -q tests/integration/test_mcp_loop.py`: one composed loop test collected.
- [CI run 36358492241](https://github.com/mcteer/coire/actions/runs/36358492241): all jobs passed; composed integration reported 107 passed and 2 skipped. The MCP loop proved cited research, prior-result plan, a committed branch, failing and missing test status, a downloadable/importable bundle, and two separate concurrent branches on the ready CI Studio. The CI engine is deterministic; a real tiny-model run remains pending.
