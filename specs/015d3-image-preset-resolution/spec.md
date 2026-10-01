# Feature Specification: Safe image preset resolution

**Feature Branch**: `feat/015d3-image-preset-resolution`
**Parent**: `specs/015-image-generation/` (part of T015, T019)
**Dependency**: draft PR #47

## Goal

Merge a published immutable image preset revision with root-level submit overrides, preserve its model binding and prompt prefix exactly once, and carry effective entitlement requirements from both preset and registry dependencies. No image admission route is enabled in this slice.

## Acceptance

1. A request bound to a preset uses only that published revision and rejects stale, retired, or mismatched model bindings.
2. Explicit request fields override permitted preset defaults; omitted fields inherit. Prompt prefix appears exactly once and cannot exceed the prompt bound.
3. Required entitlements are the union of immutable preset requirements and current dependency requirements. Explicit mode cannot be downgraded by request overrides.
4. Malformed preset data fails closed with safe errors; no imported recipe field may supply authority or acquisition behavior.
