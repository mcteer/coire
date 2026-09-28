# Feature 013 implementation record

## Dependencies

- `mcp==2.2.0` is the official Python MCP SDK, licensed MIT. It implements the current and legacy Streamable HTTP protocol versions, avoiding a custom JSON-RPC transport. Version is exact in `apps/coire-api/pyproject.toml` and `uv.lock`. Source: [official SDK release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0), [PyPI license](https://pypi.org/project/mcp/2.2.0/).

## Deployment inspection

The optional `coire-mcp` service has its own distroless image, non-root read-only runtime, dropped capabilities, 512 MiB memory cap, healthcheck, and separate compose profile. nginx routes only the exact `/mcp` endpoint.

## Validation

Ruff, mypy (389 source files), and the non-integration suite passed after OpenAPI regeneration (790 tests passed, 8 skipped, 109 integration deselected). The web suite passed (19 tests), as did ESLint and TypeScript. The local arm64 agent image built at 68.6 MB and passed all seven image-policy rules. Its Git 2.39.5 executable ran and created a branch bundle in the distroless runtime. The arm64 MCP image also passed image policy. Trivy CRITICAL scans passed for both images. [CI run 36358492241](https://github.com/mcteer/coire/actions/runs/36358492241) passed all jobs, including 107 composed integration tests with 2 skipped. The composed loop used a tiny-model acquisition and the deterministic CI fake engine. A real tiny-model run and the separate lifecycle and unverified-read composed cases remain pending.
# Coding runner dependencies

The Studio agent image adds `pytest==9.1.1` (MIT) so the initial allowlisted test runner
can execute discovered Python tests without a shell. It also ships the Git CLI from
Debian bookworm (`git=1:2.39.5-0+deb12u3`, GPL-2.0-only) for local branch, commit, diff, and bundle creation.
The CLI is a separate process and cannot reach a remote from the isolated run network;
the coding tool never invokes `push`. Both are required for the spec's reviewable apply
artifact and are absent from the core control-plane images.

## Follow-up acceptance evidence (2026-09-27)

The previously omitted composed tests now cover MCP disconnect, admin kill within five
seconds, run timeout, token revocation, MCP-only restart during chat traffic, and the
unverified-read/verified-write transition. The full targeted composed suite passed:
`COIRE_INTEGRATION=1 uv run pytest -q tests/integration/test_mcp_lifecycle.py
tests/integration/test_mcp_loop.py` — 6 passed in 323.36 seconds on the disposable local
compose project with the acquired tiny-model files and deterministic CI engine. The timeout
test exposed a relay that remained active after the run container stopped; the node now waits
for both containers to stop before reporting timeout. The disconnect test exposed that the
MCP JSON transport did not observe a dropped HTTP client; the outer ASGI receive monitor now
cancels the tool task. An apply refusal now includes the verification reason in the MCP tool
error instead of a generic crash.

The real tiny-model run against a Studio remains the final T036 gate. The running core stack
predates the 013 migration and has no MCP service enabled, so that test requires a reviewed
013 deployment plus an admin API credential. No real-model result is claimed here.

Final local static and unit gates: Ruff passed, strict mypy passed for 389 source files,
`uv run pytest -q -m 'not integration'` reported 790 passed and 8 skipped, and the OpenAPI
freshness check passed. Web tests (19), lint, and TypeScript passed. The rebuilt arm64 API,
scheduler, migrate, agent, MCP, and web images passed image policy; Trivy reported zero
CRITICAL findings for those images. No dependencies were added by this follow-up.
