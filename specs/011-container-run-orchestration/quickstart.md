# Quickstart: Validate Container Run Orchestration

## Automated gates

```bash
uv sync --all-packages --frozen
uv run ruff format --check
uv run ruff check
uv run mypy apps/ packages/
uv run pytest -q
COIRE_INTEGRATION=1 uv run pytest -q -m integration tests/integration/test_container_runs.py
uv run coire-api export-openapi --check
pnpm -C apps/coire-web test
pnpm -C apps/coire-web lint
```

The integration scenario must prove create/start/log/wait/result/remove, exact-once recovery,
kill-before-acknowledgement revocation, queueing at capacity, orphan reaping, and confinement. It
must assert no agent image or run container exists on core.

## Image gates

Build the user harness image through the repository image matrix and verify digest pinning,
non-root user, read-only-compatible filesystem, no shell/debug tools, no critical CVEs, and an SPDX
SBOM. Inspect the node-generated create payload to prove dropped capabilities, no-new-privileges,
limits, no published ports, and only the per-run internal network.

## Manual Studio verification

On the deployed feature version, submit one short run and one long run. Record:

1. the selected Studio and container inspection;
2. successful gateway access and failed internet/database/node/peer probes;
3. bounded attributed logs and collected result;
4. admin kill latency and immediate token refusal;
5. scheduler restart with exactly one container and terminal result;
6. an injected labeled orphan detected and reaped;
7. `docker ps` and image inspection on core showing no user harness image/container.

Do not perform this step from CI or a development environment. The operator runs it on the real
Studios and appends timestamps/run IDs to `review.md` without committing tokens or model data.
