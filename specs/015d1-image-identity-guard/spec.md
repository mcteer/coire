# Feature Specification: User-bound image identity guard

**Feature Branch**: `feat/015d1-image-identity-guard`
**Parent**: `specs/015-image-generation/` (part of T014, T017–T018)
**Dependency**: `015f2-image-telemetry` (draft PR #45)

## Goal

Reject image actions from non-human credentials and stale identities. Browser writes require the configured exact Origin. Personal keys require `images`; explicit actions additionally require `images:explicit` and a currently active `explicit` entitlement. Cached principal entitlements never grant a later action after revocation.

## Acceptance

1. Only active user/admin identities and personal user-bound API keys can act; legacy identity-free admin, ops, run and service credentials fail.
2. Browser mutations require exact configured Origin; personal keys do not use browser Origin but require scoped authority.
3. User, key version/revocation, scopes and entitlements are rechecked from current database state at action time.
4. Errors are safe `ImageForbidden` problem details; no prompt or credential content enters logs.
