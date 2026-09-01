# Quickstart: Observability Stack Validation

## Static gates

```bash
uv sync --all-packages
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest -q
uv run coire-api export-openapi --check
docker compose -f deploy/compose/compose.yaml config --quiet
```

## Local stack

Run `deploy/compose/coire-up`, then inspect compose health. Collector, Prometheus, Alertmanager, Loki, Tempo, and Grafana must be independently healthy. Stopping the collector must not change gateway request success.

## Trace attribution

Run the slow-stage integration fixture for each stage and query Tempo by returned trace ID. The delayed stage must be dominant, and gateway/node spans must share one trace.

## Health and alerts

Run deterministic tests for saturation, heartbeat loss, recovery hysteresis, freshness expiry, and flapping. Verify every alert expression reaches pending/firing with a subject label. Schedule real node/tunnel exercises via `docs/runbooks/observability.md` to avoid disrupting active work.

## Dashboards and production gates

Validate Cluster, Traffic, and Jobs through Grafana at desktop/mobile widths and follow Tempo/Loki links. Build all images, enforce image policy, generate SPDX SBOMs, and scan for CRITICAL vulnerabilities. On a busy Studio, verify collection CPU/RSS/duration remains inside budget or optional sampling backs off.
