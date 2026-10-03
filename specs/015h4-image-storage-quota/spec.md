# Feature Specification: Atomic image storage holds

**Feature Branch**: `feat/015h4-image-storage-quota`
**Parent**: `specs/015-image-generation/` (part of T008/T026/T035/T054)
**Dependency**: draft PR #68

## Goal

Reserve bounded owner and global image storage before accepting an input or output, and release or settle that hold exactly once in the same database transaction as its owning row.

## Acceptance

1. A transaction obtains owner/global quota rows in one lock order. Missing rows are created without a concurrent first-use race.
2. A storage hold checks positive requested bytes, owner/global `stored + held` caps, and the physical disk safety floor before incrementing both counters.
3. Settling a hold can move only its reserved bytes to stored usage. Releasing a failed hold decrements both counters; mismatched or repeated releases fail closed.
4. Tests cover exact caps, owner/global exhaustion, physical floor, wrong owner, over-settlement and rollback-safe in-memory behavior. Cross-process PostgreSQL concurrency evidence remains required before admission is enabled.
5. No route calls this ledger yet; image admission remains disabled.
