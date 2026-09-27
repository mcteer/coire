# Quickstart Validation: Admin Console

Use `feat/008-admin-console`, `uv sync --all-packages`, and `pnpm -C apps/coire-web install`. Automated tests use seeded/fake nodes, never real Studios.

```bash
uv run ruff format --check
uv run ruff check
uv run mypy packages apps
uv run coire-api export-openapi --check
pnpm -C apps/coire-web test
pnpm -C apps/coire-web lint
pnpm -C apps/coire-web build
uv run pytest -q apps/coire-api/tests/contract/test_admin_console.py
uv run pytest -q apps/coire-api/tests/unit/test_console_service.py
uv run pytest -q tests/integration/test_admin_console.py -m integration
```

Validate admin/non-admin behavior, stream reconnect/reconcile, fresh/degraded/unreachable node data, concurrency conflict, audited stops, one-time keys, pagination and Ask Coire degradation.

For browser validation, run the composed seeded control plane and use Playwright WebKit at 1440×900 and below 1200 px. Verify the Glass mockup, focus/keyboard behavior, all shipped tabs, named confirmations, reconnection, and absence of Agent Runs/Upgrades controls.

Finally build API/web/migrate images, run image policy, produce SPDX SBOMs, scan for critical vulnerabilities, and follow `docs/runbooks/admin-console.md` for observation and rollback.
