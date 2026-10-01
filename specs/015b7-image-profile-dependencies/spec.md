# Feature Specification: Required image profile dependencies

**Feature Branch**: `feat/015b7-image-profile-dependencies`
**Parent**: `specs/015-image-generation/` (part of T006, T015, T019, T027)
**Dependency**: draft PR #52

## Goal

A measured image base profile must name immutable required registry dependencies. Preset resolution must include those dependencies even if they are absent from a visible preset or removed by request overrides.

## Acceptance

1. `ImageCapabilityProfile` has a bounded, duplicate-free UUID list of required dependencies, with an empty default for existing profiles.
2. Stored preset resolution loads every required base-profile dependency, verifies ready image-kind registry state, and includes its current entitlements in the admission union.
3. Missing or malformed hidden dependencies fail closed before live admission.
4. OpenAPI and generated TypeScript types reflect the additive field.
