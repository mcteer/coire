# Feature 012 Review Evidence

## Dependency evidence

- `fastapi==0.135.1` — MIT — internal typed HTTP and health boundary for the long-lived ops-only
  distribution. It is an optional `coire-agent[ops]` dependency and is absent from the Studio user
  harness image.
- `uvicorn==0.41.0` — BSD-3-Clause — single-process ASGI runtime for that boundary. It is likewise
  ops-only. Both exact versions were already locked for `coire-api`; no new transitive package or
  licence was introduced.

## Validation evidence

Recorded 2026-09-01 on Apple Silicon against commit `2826697` plus the gate fixes in the working
tree. No credentials, confirmation tokens, prompts, or model contents are recorded here.

- Migration reversibility: disposable PostgreSQL 17 completed `upgrade head`, `downgrade -1`, and
  `upgrade head`; `alembic current` reported `0012_ops_confirmations (head)`.
- Python unit/contract suite: `645 passed, 8 skipped, 106 deselected` with the integration marker
  excluded. Strict mypy passed for all 311 files under `apps` and `packages`; repository Ruff format
  and lint checks passed.
- Web: all 13 Vitest tests passed; ESLint, `tsc --noEmit`, and the Vite production build passed.
- OpenAPI: `uv run python -m coire_api.openapi --check` passed after regenerating
  `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`.
- Composed quickstart: all four scenarios in `tests/integration/test_ops_confirmations.py` passed in
  131.19 seconds after convergence remediation. Evidence covers exact confirmation and decline,
  concurrent single use, replay,
  expiry, stale state, prior-session invalidation, irreversible-action rejection, zero-inference
  degraded status/refusal, automatic model-backed recovery without an ops restart, and core process
  isolation.
- Image build/policy: `coire-agent-ops:ci` (`linux/arm64`) passed rules 1–7: no shell or package
  manager, non-root `65532:65532`, read-only-root compatible, arm64, exec-form entrypoint, and
  digest-pinned bases.
- Security scan: Trivy found zero CRITICAL findings in Debian 12.15 and every Python package in the
  ops image.
- SBOM: Syft generated a non-empty 1,230,583-byte SPDX JSON document in a temporary path (not
  committed). It reports `fastapi==0.135.1` as MIT and `uvicorn==0.41.0` as BSD-3-Clause.

## Convergence evidence

The first non-destructive audit found three gaps: stale session generations were not bounded by
`OPS_SESSION_STALE_S`, execution dispatch was not directly covered for every allowlisted action,
and 409 confirmation/decline responses were absent from OpenAPI. T066–T068 remedied those gaps.
A repeat requirement-by-requirement audit mapped FR-001–FR-020 and SC-001–SC-008 to code, direct
tests, composed evidence, image inspection, or deployment policy and found no remaining ambiguity,
duplication, constitutional conflict, or unimplemented behavior.

## Current CI evidence — 2026-09-02

GitHub Actions run `33595020338` for commit `67a0112` passed every required gate: Ruff and strict
mypy, Python and web tests, digest-pinned production image builds with policy/CVE/SBOM checks, the
engine suite, and the full composed integration suite (`103 passed, 2 skipped`). The integration
run includes the sharding outage/fallback scenario after the cleanup-order stabilization.

The follow-up run `33599562276` for commit `b161e80` also passed every required gate. Its full
composed integration suite completed with `104 passed, 2 skipped` in 674.11 seconds, including the
pre-group sharded-admission cleanup regression. Engine validation and all image policy, CVE, SBOM,
lint, type, web, and unit/contract checks were green.
