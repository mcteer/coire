# Feature Specification: Image migration verification

**Feature Branch**: `feat/015c4-image-migration-verification`
**Parent**: `specs/015-image-generation/` (part of T008–T009)
**Dependency**: `015c3-image-capacity-schema` (draft PR #40)

## Goal

Prove the 0023–0025 image migrations on a disposable local PostgreSQL server, including compatibility with existing text/VLM rows, database constraints and rollback safeguards.

## Acceptance

1. Existing text/VLM registry rows survive upgrade and empty-schema downgrade unchanged.
2. Postgres rejects invalid image job IDs, duplicate owner idempotency keys, mismatched output owners and recipe-only inputs with normalized assets.
3. Occupied image tables block downgrade without losing the current migration head.
4. No real Studio or long-lived database is used.
