# Feature Specification: Live image preset lookup

**Feature Branch**: `feat/015d4-image-preset-store`
**Parent**: `specs/015-image-generation/` (part of T015, T017, T019)
**Dependency**: draft PR #50

## Goal

Resolve a saved image preset from its immutable revision row and current registry dependencies in one authorization transaction. A stale, retired, missing, wrong-kind or unverified dependency must stop admission before any engine work. No production route is enabled in this slice.

## Acceptance

1. A published current preset revision resolves with its persisted defaults, prefix, frozen dependencies and entitlement requirements.
2. Missing, retired, wrong-kind or non-ready base/auxiliary registry rows fail closed.
3. Current registry requirements are unioned with stored requirements; live identity and entitlement checks occur in the same session before the result returns.
4. Invalid stored JSON or dependency identifiers fail with safe image errors rather than engine details.
