# Planned validation guide

This is not implementation evidence. Pause after plan/tasks/analyze per the user's instruction. Resume on feat/023-control-plane-efficiency and close the governance prerequisite before code.

## Prerequisites and routine gates

Python 3.13/uv, pnpm, local OrbStack and pinned ARM64 images. Use unique coire-it-* projects and private temporary state/secrets; no Studio Docker/engine operations or model downloads.

```sh
uv sync --all-packages
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest -q -m 'not integration'
pnpm -C apps/coire-web test
pnpm -C apps/coire-web lint
pnpm -C apps/coire-web build
uv run coire-api export-openapi --check
```

Regenerate OpenAPI/TS when contracts change. Regression tests precede implementation.

## Deployment and credentials

Test late missing secrets leave active files unchanged; check shared project locks and production/test isolation; missing bind files fail before Docker creates directories; source changes cannot affect an installed release. Seed a row in local PostgreSQL, introduce credential mismatch, verify application-network preflight fails despite pg_isready, explicitly reconcile through protected input, and verify both the row and working auth survive.

## Workers and console

Run new lifecycle/backoff/kill tests and existing acquisition/placement/sharding/run integration. Block WAIT then request kill: token rejects immediately and responsive-node stop completes within five seconds. Repeat with another node unavailable. Failed kill cannot generate completion state/audit. Restart scheduler and verify adoption without duplication; race CREATE/normal completion against kill without token revival.

Connect two authorized console clients and count projections; advance only envelope time then real state/freshness. Hide/show/offline/reconnect the page and verify cleanup, bounded retry and reconciliation. Check runtime labels and unsupported CPU null.

## Profiles, images and local remediation

Render all supported Compose profile combinations. Exercise lean and diagnostics ingestion/alerts/real HTTP health; turning diagnostics off leaves no exporter retry traffic. Run changed-image builds, policy/scans/SBOM gates; executable version output is not readiness evidence.

After implementation gates, capture current core state/data and prepare a stable release/credential generation. Prove mismatch before explicit reconciliation, recreate only affected core services, verify authenticated health and before/after 60-second auth/export error rates plus resource samples. Desktop-frame improvement requires reproduction; OrbStack-stop comparison is a scheduled interruption, not automatic remediation.

Rollback selects a previous validated release while retaining volume identity and deliberately reconciling any changed database credential. Never reset data or wholesale regenerate secrets as a shortcut.
