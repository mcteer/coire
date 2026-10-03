# Feature Specification: Image node and worker commands

**Feature Branch**: `feat/015b4-image-worker-contracts`
**Parent**: `specs/015-image-generation/` (T005; node-message part of T007)
**Dependency**: `015b3-image-registry-contracts` (draft PR #34)

## Goal

Define strict, versioned commands for scheduler-to-node and node-to-worker image execution. Every command binds a registry model, ULID job, attempt and fence. Unknown fields, caller paths, invalid IDs, mismatched receipts and non-mflux backends fail validation. No route or worker process is enabled by this child.

## Acceptance

1. Load accepts only a registry-selected mflux base and a digest, not an import target, URL or path.
2. Start/run, status, cancel, input transfer and cleanup shapes bind job, attempt and fence; duplicate or mismatched receipt IDs are invalid.
3. Transfer grants are node/attempt/output bound and expire; receipts carry exact digest/size and no raw bytes.
4. Commands use shared coire-core Pydantic models with `extra="forbid"`.
