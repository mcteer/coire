# Feature Specification: Durable Studio image job journal

**Feature Branch**: `feat/015e17-image-node-journal`
**Parent**: `specs/015-image-generation/` (part of T024/T032)
**Dependency**: draft PR #86

## Goal

Persist the exact node image attempt and last safe status before worker dispatch, so node restarts and repeated scheduler calls observe the existing attempt without blindly generating again.

## Acceptance

1. A private node-owned journal accepts one immutable `NodeImageStartRequest` per job ID. An identical retry returns the same `NodeImageJob`; a changed request, attempt or fence is refused.
2. Journal reads validate bounded, owner-only files and do not deserialize a caller-supplied path. Corrupt/uncertain records prevent a replacement attempt and preserve evidence.
3. Status updates are atomic and cannot change attempt identity or move a terminal job back into an active state. Recovery returns the persisted safe state and does not call the worker.
4. Tests exercise replay, conflict, restart, corrupt state and terminal transition. No native engine is run.
