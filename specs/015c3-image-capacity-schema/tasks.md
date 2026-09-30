# Tasks: Image capacity and lease persistence

- [X] C001 Add failing quota, lease and migration tests.
- [X] C002 Add typed ORM rows and reversible 0025 migration.
- [X] C003 Run repository gates and document rollback.

## Verification

- Focused capacity schema tests: 3 passed after expected initial failures.
- Full Python suite: 1,242 passed, 141 existing skips.
- Ruff, strict mypy across 494 files, OpenAPI freshness and Alembic offline upgrade passed.
- Live PostgreSQL migration awaits a disposable local database; no cluster migration run.
