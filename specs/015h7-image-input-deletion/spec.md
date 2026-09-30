# Feature Specification: Owner image input deletion

**Feature Branch**: `feat/015h7-image-input-deletion`
**Parent**: `specs/015-image-generation/` (part of T039/T054)
**Dependency**: draft PR #71

## Goal

Let the owner remove a recipe input, including a ready one, without exposing its bytes or releasing storage capacity before physical deletion.

## Acceptance

1. `DELETE /api/v1/image-inputs/{id}` requires the current image user and browser origin policy. It returns 202 with the existing `ImageInput` projection after committing a tombstone. Other owners and unknown IDs get the same 404. A repeated owner request is safe, including after purge.
2. New owner status reads and recipe use treat a tombstoned input as absent. A processing parser may finish its file read, but its final state check cannot publish a recipe after deletion commits.
3. An input with active job references returns 409 without changing state. Job cancellation and reference draining are part of the later generation-job lifecycle; deletion does not silently remove live job sources.
4. API maintenance purges tombstoned recipe originals with strict generated-name, regular-file and no-follow checks. It releases held or stored owner/global quota only after unlink or confirmed absence, then records `purged_at` and `state=purged` in one transaction. Missing files and retries are safe; failed purge retains quota.
5. Fixed-label cleanup telemetry and the 24-hour oldest-pending alert cover tombstoned inputs. Tests cover authorization, idempotency, active references, parser race, quota order, missing file and symlink refusal. Image admission remains disabled by default.
