# Feature Specification: Owner recipe-only upload admission

**Feature Branch**: `feat/015h5-image-recipe-upload`
**Parent**: `specs/015-image-generation/` (part of T012/T049/T050/T054)
**Dependency**: draft PR #69

## Goal

Accept an owner-scoped recipe-only PNG up to 64 MiB, reserve its exact stored bytes, persist a durable processing row and let the scheduler obtain its strict recipe from the private file worker.

## Acceptance

1. POST `/api/v1/image-inputs` requires the live human/personal image credential, valid origin for browser mutation, purpose `recipe`, a safe filename and declared byte count. The actual bytes are bounded and hashed by private staging.
2. Admission reserves owner/global quota and commits a processing `ImageInputRow` before returning a typed 202 receipt. Pre-commit failures discard staged bytes; an uncertain commit retains the generated file for reconciliation. The stored original key is a generated UUID, never a filename.
3. Scheduler recovery scans processing recipe rows and starts a DBOS workflow. The workflow calls the typed private parser with row ID, committed size and SHA-256; on success it verifies the response binding again, stores the strict recipe, and settles the hold exactly once. On stable parser refusal it records a safe failure while retaining the hold until physical cleanup.
4. A worker outage or busy response keeps the row processing for retry. Repeated workflow execution on a ready row has no effect. The public route does not accept generation sources in this slice.
5. Tests cover auth, size mismatch, quota refusal, safe rollback, worker response identity, recovery and idempotent settlement. `COIRE_IMAGE_ENABLED` remains false by default.
