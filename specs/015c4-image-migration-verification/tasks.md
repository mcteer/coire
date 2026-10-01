# Tasks: Image migration verification

- [X] C001 Add a disposable PostgreSQL migration and constraint test.
- [X] C002 Run it against a local PostgreSQL 17 container and fix failures.
- [X] C003 Run repository gates, stop the test container and record evidence.

## Verification

- Disposable local PostgreSQL 17 via OrbStack: migration test passed, including 0022→0025→0022, text/VLM rows, owner/idempotency/purpose constraints, and occupied downgrade refusal.
- Full Python suite with local PostgreSQL available: 1,246 passed, 138 unrelated conditional skips.
- Ruff, strict mypy across 495 files and OpenAPI freshness passed.
- The disposable container was stopped and removed after testing; no Studio was contacted.
