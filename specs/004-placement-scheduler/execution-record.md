# Feature 004 execution record

Local and composed validation completed 2026-08-31 on Apple Silicon.

- Ruff format/check: passed.
- strict mypy: 153 source and test files passed.
- Python tests: 456 passed, 90 skipped (engine/integration markers excluded by default).
- Web: 2 Vitest tests passed; ESLint passed.
- OpenAPI freshness and regenerated TypeScript schema: passed.
- Composed control plane: 85 passed, 1 intentionally skipped engine-only case in 473.96s.
- Production images `coire-api:dev`, `coire-scheduler:dev`, and `coire-migrate:dev` built and
  passed all six image-policy rules.
- Trivy 0.73.0: zero CRITICAL findings in all three changed images.

The composed placement scenario used two verified models, constrained Studio A's authoritative
budget, proved a pinned occupant caused an actionable refusal, unpinned it, restarted the
scheduler during the next decision, and observed confirmed LRU unload before admission.

The real-cluster ≤1 GB/no-swap and three-node quickstart remain pending until the updated node
processes and core images are activated together. No live-engine command was issued from CI.
