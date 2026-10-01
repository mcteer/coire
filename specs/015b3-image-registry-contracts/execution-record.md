# Execution record: image registry kinds

- Focused registry contracts: 7 passed, no skips.
- Full Python suite: 1205 passed, 141 existing skips.
- Repository Ruff format/check and mypy: passed (484 source files).
- OpenAPI freshness: passed; checked-in OpenAPI and TypeScript types regenerated.
- Web: 93 tests passed; ESLint, TypeScript typecheck and both pnpm/npm lock installs passed.
- No image worker can start via the generic engine request. Model acquisition, persistence, routing and actual image jobs remain disabled.
