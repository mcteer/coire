# Execution record: image node and worker commands

- Focused command tests: 5 passed, no skips. Combined with the prior registry child, T005 covers legacy defaults, auxiliary exclusion, UUID/ULID validation, attempt/fence mismatch, backend discrimination and node-bound receipts.
- Full Python suite: 1210 passed, 141 existing skips.
- Repository Ruff format/check and mypy: passed across 486 source files.
- OpenAPI freshness: passed; these commands are not exposed by any route yet.
- No image execution route or process is enabled. Acquisition, isolated parser and admin-console shape extensions remain parent T007 work.
