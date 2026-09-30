# Feature Specification: Image routing isolation acceptance

**Feature Branch**: `feat/015e5-image-routing-regression`
**Parent**: `specs/015-image-generation/` (completes T028)
**Dependency**: draft PR #60

## Goal

Close the remaining regression coverage for image/auxiliary exclusion from existing language, vision, model listing, and signed failover paths.

## Acceptance

1. Every image asset kind stays absent from chat eligibility even if a malformed row has a language backend.
2. The failover snapshot query and its in-memory defensive projection exclude image and auxiliary rows.
3. Existing language and vision models remain routable; image acquisition continues through its separate, unfinished admin path.
