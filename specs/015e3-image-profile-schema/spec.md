# Feature Specification: Persist measured image capability

**Feature Branch**: `feat/015e3-image-profile-schema`
**Parent**: `specs/015-image-generation/` (part of T027)
**Dependency**: draft PR #50

## Goal

Give image base models a dedicated persisted capability profile matching the existing wire contract. A ready image base must have a profile, and non-base kinds cannot carry one. Downgrade must refuse to discard stored profiles.

## Acceptance

1. Existing registry rows gain a nullable profile without changing their kind/backend/state.
2. The database refuses ready image bases without a profile and refuses profiles on language or auxiliary kinds.
3. The ORM exposes the field; route/service code can parse it through `ImageCapabilityProfile` later.
4. A downgrade with any non-null profile refuses; clean downgrade removes the field and constraint.
