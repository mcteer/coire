# Feature 005 — Model instances and cluster state

Spec: `specs/005-instances-cluster-state/spec.md`

## Summary

- Adds a durable `ModelInstance` lifecycle with persisted transitions, restart recovery,
  bounded drain, instance-scoped reservations and multiple ready copies of one model.
- Adds authenticated instance, event-stream and typed cluster-state APIs; gateway routing now
  prefers affinity and then least in-flight load.
- Replaces registration-time row creation and static registration-token acceptance with
  admin-declared nodes and hashed, revocable, single-use bootstrap credentials.
- Adds lifecycle/registration telemetry, alerts, a dashboard panel and an operator runbook.

## Constitution compliance

- **I — Bare engines:** only coire-node starts and proxies the existing bare MLX engine command;
  no inference wrapper or externally reachable engine port was added.
- **II — Core hosts no models:** core persists and schedules lifecycle state only; model members
  and Metal work remain on declared Studio nodes.
- **II-a — One service, one container:** no service was combined; changed images remain non-root,
  shell-free, read-only compatible, capability-dropped by Compose and healthchecked.
- **III — Contracts first:** instance, event, member, cluster-state, declaration and credential
  wire shapes are strict Pydantic models in coire-core; OpenAPI and TypeScript types are fresh.
- **IV — Zero implicit trust:** all management routes are scoped, transitions and registration
  outcomes are audited, plaintext bootstrap tokens are never persisted, and unknown, invalid,
  revoked or consumed credentials are refused.
- **V — Models are data:** callers select registry model/variant identifiers; engine paths are
  resolved from verified copies and never accepted from request input.
- **VI — Observable:** lifecycle and registration paths emit `coire.*` spans, `coire_*` metrics,
  structured identity fields, Grafana coverage and alert rules.
- **VII — Spec-driven, test-gated:** implementation follows this spec/plan/tasks set and includes
  contract, unit and composed lifecycle tests.

## Validation evidence (2026-09-01)

- Ruff format/check: 292 files clean.
- Strict mypy: 97 active core/API/node source files clean.
- Unit/contract: 473 passed, 91 integration-only skipped.
- Web: 2 tests passed; ESLint clean.
- OpenAPI freshness: clean; generated TypeScript schema updated.
- Full composed suite: 86 passed, 1 optional skip, 477 deselected in 539.99 s.
- Focused feature suite: 21 passed in 185.97 s.
- Image policy: API, scheduler and migrate pass all seven rules.
- Trivy 0.73 CRITICAL scan: zero findings in API, scheduler and migrate.

## Operational rollout

Follow `docs/runbooks/instances.md` and `specs/005-instances-cluster-state/quickstart.md`.
The live three-node quickstart remains an explicit post-deploy gate and must be recorded before
merge; it is intentionally not represented as complete by the local composed evidence above.
