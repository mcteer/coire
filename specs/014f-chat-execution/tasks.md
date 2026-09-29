# Tasks: Shared Gateway Stream Execution

- [X] F001 Add direct regression tests for usage parsing, credential revocation, cancellation, model rewrite and first-token metrics in `apps/coire-api/tests/unit/test_gateway_execution.py` and existing `test_gateway_auth.py`.
- [X] F002 Extract tracking, cancellation response and model rewrite to `apps/coire-api/src/coire_api/gateway/execution.py`, re-exporting compatibility helpers from `routes/v1.py`.
- [X] F003 Run Ruff, strict mypy, gateway tests, full unit/contract suite, OpenAPI freshness and web build.
