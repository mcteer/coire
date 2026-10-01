# Feature Specification: Private image asset persistence

**Feature Branch**: `feat/015c2-image-asset-schema`
**Parent**: `specs/015-image-generation/` (part of T008–T009)
**Dependency**: `015c1-image-job-schema` (draft PR #38)

## Goal

Persist private inputs, outputs, transfer receipts and short-lived download grants. Keep generation and recipe-only input purposes distinct, and enforce owner relationships and output slot uniqueness in Postgres. No image route is enabled.

## Acceptance

1. Inputs carry purpose, owner, digest, actual bytes and lifecycle timestamps; recipe-only inputs cannot masquerade as normalized generation inputs.
2. Output owner matches its job; output indices and transfer receipts are unique per job attempt/slot.
3. Grants contain only a hash and require an output belonging to the same owner.
4. An additive migration follows 0023 and refuses destructive downgrade while image asset records exist.
