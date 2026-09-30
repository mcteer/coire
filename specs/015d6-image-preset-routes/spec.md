# Feature Specification: Human-admin image preset routes

**Feature Branch**: `feat/015d6-image-preset-routes`
**Parent**: `specs/015-image-generation/` (part of T014, T020)
**Dependency**: draft PR #54

## Goal

Expose audited create/update/retire routes for image presets only to active human administrators. Browser mutations require the configured exact Origin. The routes commit the pointer, immutable revision and success audit atomically; refusals are audited separately.

## Acceptance

1. POST/PATCH/DELETE use the existing strict Pydantic contracts and appear in generated OpenAPI/TypeScript types.
2. Personal/API, ops, run and legacy no-user credentials cannot mutate presets, even if granted a generic admin scope.
3. A demoted/inactive user or wrong Origin is refused before service work, with a fixed-content refusal audit.
4. Successful mutations commit service and success audit together; stale revision and duplicate name become safe conflicts.
