# Tasks: Private image asset persistence

- [X] C001 Add failing owner/uniqueness/purpose and migration guard tests.
- [X] C002 Add typed ORM rows and reversible 0024 migration.
- [X] C003 Run repository gates and document rollback.

## Verification

- Focused asset schema tests: 3 passed after expected initial failures.
- Full Python suite: 1,239 passed, 141 existing skips.
- Ruff, strict mypy across 492 files, OpenAPI freshness and Alembic offline upgrade passed.
- Live PostgreSQL migration awaits a disposable local database; no cluster migration run.
