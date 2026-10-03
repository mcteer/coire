# Feature Specification: Disabled image storage topology

**Feature Branch**: `feat/015f1-image-storage-config`
**Parent**: `specs/015-image-generation/` (T012)
**Dependency**: `015c4-image-migration-verification` (draft PR #41)

## Goal

Prepare private core image storage and narrowly bounded ingress without enabling admission. Only the API receives the final blob volume; the isolated file worker keeps its existing chat volumes with dedicated image subpaths.

## Acceptance

1. Compose maps all bounded image settings to API and relevant scheduler settings, defaulting admission off; integration overlay keeps it off.
2. `coire-blobs` is mounted read-write only by coire-api; no shell, network or capability changes.
3. Nginx permits at most a 65 MiB multipart envelope only on image-input upload, while application purpose limits are 10 MiB generation and 64 MiB recipe; internal output PUT is bounded to 64 MiB.
4. Existing chat and generic API upload limits do not widen.
