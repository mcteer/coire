# Tasks: Private image job and preset persistence

- [X] C001 Add failing schema and downgrade-guard tests.
- [X] C002 Add typed ORM rows and the reversible 0023 migration.
- [X] C003 Run repository checks and record evidence.

## Verification

- Focused persistence tests: 3 passed after expected initial failures.
- Full Python suite: 1,236 passed, 141 existing skips.
- Ruff and mypy passed for changed Python files; Alembic offline upgrade SQL generated.
- Live PostgreSQL upgrade/downgrade awaits a disposable local database; no cluster migration run.
