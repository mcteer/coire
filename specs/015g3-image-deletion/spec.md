# Feature Specification: Owner output deletion and verified purge

**Feature Branch**: `feat/015g3-image-deletion`
**Parent**: `specs/015-image-generation/` (part of T026/T038/T039)
**Dependency**: draft PR #67

## Goal

Let the output owner tombstone a published image so new reads fail immediately, then remove its private blob and release charged storage no later than 24 hours.

## Acceptance

1. DELETE `/api/v1/image-outputs/{id}` requires the live owner credential, locks the output, sets `deleted_at`, and returns a typed 202 receipt. Repeated owner deletes are idempotent; another user sees 404.
2. Gallery and grant/content guards treat tombstoned rows as absent immediately. A stream authorized before the tombstone may finish.
3. A bounded API maintenance pass safely removes the blob under the configured root without following symlinks. Missing bytes are treated as already removed; unsafe paths fail closed.
4. Only after physical removal does the pass set `purged_at` and release owner/global stored-byte counters. A failed pass remains retryable and emits bounded telemetry. The oldest pending purge age is observable.
5. Tests cover owner isolation, idempotent tombstones, safe path handling, missing bytes and delayed quota release. Existing image admission stays disabled.
