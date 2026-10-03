# Tasks: User-bound image identity guard

- [X] C001 Add failing principal, scope, Origin and revocation tests.
- [X] C002 Add preflight and live authorization functions.
- [X] C003 Run repository gates and record evidence.

## Verification

- Focused authorization tests: 11 passed after expected import failure.
- Full Python suite: 1,258 passed, 142 existing conditional skips.
- Ruff, strict mypy across 500 files and OpenAPI freshness passed.
- No image route is enabled. Owner-row filtering, audit, revocation cancellation and Postgres integration remain parent tasks.
