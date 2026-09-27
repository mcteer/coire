# Feature 013 implementation record

## Dependencies

- `mcp==2.2.0` is the official Python MCP SDK, licensed MIT. It implements the current and legacy Streamable HTTP protocol versions, avoiding a custom JSON-RPC transport. Version is exact in `apps/coire-api/pyproject.toml` and `uv.lock`. Source: [official SDK release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0), [PyPI license](https://pypi.org/project/mcp/2.2.0/).

## Deployment inspection

The optional `coire-mcp` service has its own distroless image, non-root read-only runtime, dropped capabilities, 512 MiB memory cap, healthcheck, and separate compose profile. nginx routes only the exact `/mcp` endpoint.

## Validation

Ruff, mypy (389 source files), and the non-integration suite passed after OpenAPI regeneration (790 tests passed, 8 skipped, 109 integration deselected). The web suite passed (19 tests), as did ESLint and TypeScript. The local arm64 agent image built at 68.6 MB and passed all seven image-policy rules. Its Git 2.39.5 executable ran and created a branch bundle in the distroless runtime. The arm64 MCP image also passed image policy. Trivy CRITICAL scans passed for both images. The composed MCP loop test collects successfully, but CI execution and a real tiny-model run remain pending.
# Coding runner dependencies

The Studio agent image adds `pytest==9.1.1` (MIT) so the initial allowlisted test runner
can execute discovered Python tests without a shell. It also ships the Git CLI from
Debian bookworm (`git=1:2.39.5-0+deb12u3`, GPL-2.0-only) for local branch, commit, diff, and bundle creation.
The CLI is a separate process and cannot reach a remote from the isolated run network;
the coding tool never invokes `push`. Both are required for the spec's reviewable apply
artifact and are absent from the core control-plane images.
