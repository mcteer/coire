# Feature Specification: Audited image preset mutations

**Feature Branch**: `feat/015d5-image-preset-admin`
**Parent**: `specs/015-image-generation/` (part of T015, T019–T020)
**Dependency**: draft PR #53

## Goal

Human administrators can create, revise and retire named image presets using immutable revision rows. Each published revision freezes current base-profile and visible auxiliary dependencies and entitlement requirements. No production route is enabled in this service slice.

## Acceptance

1. Create requires a ready measured image base and ready correctly typed auxiliary dependencies; it writes a published pointer and immutable revision 1.
2. Update compares the expected current revision under lock, inserts a new immutable revision, and never edits prior revision payload.
3. Retire prevents new lookup while retaining revision history for existing jobs.
4. Each successful mutation writes a content-free audit in the same transaction; invalid dependency, stale update or missing preset fails safely.
