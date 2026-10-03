# Tasks: Content-free image telemetry

- [X] C001 Add failing API/node label and dashboard/alert tests.
- [X] C002 Add bounded helpers and provisioned observability artifacts.
- [X] C003 Run Python/config checks and record evidence.

## Verification

- Focused API/node label and provisioning tests: 3 passed after expected import failure.
- Full Python suite: 1,247 passed, 142 existing conditional skips.
- Ruff, strict mypy across 498 files and OpenAPI freshness passed.
- Promtool validated all three image alert rules. Dashboard JSON parsed in tests.
- No image route or worker path is enabled; later services must call these helpers.
