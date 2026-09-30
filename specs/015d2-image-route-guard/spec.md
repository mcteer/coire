# Feature Specification: Audited image route and owner guard

**Feature Branch**: `feat/015d2-image-route-guard`
**Parent**: `specs/015-image-generation/` (part of T014, T017–T018)
**Dependency**: `015d1-image-identity-guard` (draft PR #46)

## Goal

Expose a reusable FastAPI image dependency that audits refusals before an image route starts work. Ordinary job, input and output lookups must use owner-only semantics even for administrators. No production image route is enabled yet.

## Acceptance

1. Human browser mutations with a wrong/missing Origin and service/unscoped credentials are refused with safe problem details and a content-free refusal audit.
2. Allowed callers pass preflight and current database authorization; a refused live recheck also audits.
3. Ordinary job/input/output lookup returns 404 for non-owner, deleted or unpublished content, regardless of admin role.
4. The dependency is reusable by later image routes without widening generic auth.
