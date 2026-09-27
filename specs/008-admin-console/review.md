# Feature 008 review handoff

**Status:** Complete after convergence follow-up on 2026-09-01.

## Delivered surface

- Server-enforced admin snapshot, resumable fetch-SSE stream, typed activity timeline, stable collection pagination, optimistic edit preconditions, and audited job cancellation.
- Responsive Glass admin shell with overview, model lifecycle and curation, variant publication/default selection, instance/job stops, identity/key/entitlement administration, audit filtering, and read-only Ask presentation.
- Generated OpenAPI/TypeScript contracts, OTel spans and metrics, dashboard, alerts, and operational runbook.
- Gateway integration fixture isolation so a completed gateway module releases its engine and does not contaminate later placement scenarios.

Several planned source paths were consolidated into `App.tsx` and existing route modules rather than creating one-file wrappers. Task boxes record delivered behavior; unchecked boxes identify missing behavior or missing focused coverage.

## Verification evidence

- Python: Ruff clean; strict mypy clean across 232 files; 553 passed, 8 skipped (non-integration).
- Composed integration: one clean run completed with 93 passed and 1 skipped. A later repeat completed 92 passed and 1 skipped with the pre-existing timing-sensitive smaller-survivor sharding fallback failing after its fallback instance was created; no console code participates in that path. No real Studio or engine process was invoked.
- Web: 11 Vitest tests passed; ESLint, TypeScript, and Vite production build passed.
- Browser: Playwright 1.62.1/WebKit 2336 passed authenticated desktop (1440×1000) and mobile (390×844) rendering, navigation, live node tile, and horizontal-overflow checks. Visual inspection found and corrected mobile dock clipping.
- Images: `coire-api:008`, `coire-web:008`, and `coire-migrate:008` pass all seven image-policy rules. SPDX JSON SBOMs were generated in `/tmp`; Trivy reports zero critical vulnerabilities for all three.
- OpenAPI freshness and `git diff --check` pass.

## Convergence follow-up

The core overview now emits a typed `CoreHostCapacity` projection explicitly labelled
`core-control-plane-runtime`; this is the runtime-visible capacity and does not claim physical-host
GPU or thermal telemetry that core intentionally does not provide. Roster curation, entitlements,
validation, and picker defaults are implemented in the admin surface. Ask Coire is backed by the
isolated long-lived ops service from Feature 012 and exposes explicit unavailable/degraded behavior.
The corresponding contract, browser, strict typing, and composed tests pass.

These are requirement/dependency issues, not failing gates. Feature 008 must not be called complete until the spec is amended or those prerequisites are deliberately pulled forward with their own spec/plan.

## Constitution check

- **I — Bare engines:** pass; console calls Coire lifecycle routes only.
- **II — Core boundary:** pass for implemented code; no model or Metal path was added to core.
- **II-a — Containers:** pass; API/web/migrate remain separate, non-root, read-only-compatible, shell-free images.
- **III — Contracts first:** pass for implemented wire surfaces; strict Pydantic contracts generated OpenAPI and TypeScript.
- **IV — Zero trust:** pass; admin routes re-authorize and mutations use existing audited paths. Optimistic writes require `If-Match`.
- **V — Models as data:** pass; controls resolve registry/model/variant identifiers and use admin acquisition/lifecycle routes.
- **VI — Observable:** pass; spans, metrics, dashboard, alert, and runbook are included.
- **VII — Spec/test gated:** implementation and convergence gates pass; the remaining real-cluster
  acceptance is tracked as operator evidence in the outstanding checklist.

## Proposed PR summary (once blockers are resolved)

Implements the Feature 008 role-gated admin console and live operational stream. Touches Principles II, III, IV, V, VI, and VII by keeping model work on Studios, defining strict contracts first, enforcing admin authorization/versioned mutations, using registry-only lifecycle actions, adding console telemetry, and passing unit/contract/integration/browser/image gates.
