# Feature 013 implementation record

## Dependencies

- `mcp==2.2.0` is the official Python MCP SDK, licensed MIT. It implements the current and legacy Streamable HTTP protocol versions, avoiding a custom JSON-RPC transport. Version is exact in `apps/coire-api/pyproject.toml` and `uv.lock`. Source: [official SDK release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0), [PyPI license](https://pypi.org/project/mcp/2.2.0/).

## Deployment inspection

The optional `coire-mcp` service already has its own distroless image, non-root read-only runtime, dropped capabilities, 512 MiB memory cap, healthcheck, and separate compose profile. nginx currently routes `/mcp/`; task T023 will make the exact `/mcp` endpoint explicit.

## Validation

Pending implementation and tests. No acceptance gate is recorded as passed yet.
