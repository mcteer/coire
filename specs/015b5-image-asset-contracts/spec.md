# Feature Specification: Image asset validation and parser contracts

**Feature Branch**: `feat/015b5-image-asset-contracts`
**Parent**: `specs/015-image-generation/` (remaining T007)
**Dependency**: `015b4-image-worker-contracts` (draft PR #35)

## Goal

Finish shared image acquisition, isolated file-worker and admin activity shapes. Existing text/VLM contracts retain their defaults. New parser operations are separate commands until service support exists, so an image request cannot accidentally reach the existing chat file-worker path.

## Acceptance

1. Acquisition inspection/validation identifies an image asset kind, backend and verified image capability only for a generation base; legacy text/VLM defaults remain valid.
2. Image file-worker commands distinguish bounded generation inputs from recipe-only imports, reject paths and incompatible operations, and cap extracted metadata at 64 KiB.
3. Admin image activity carries ULID job identity and bounded, content-free fields without changing the existing UUID job/instance activity shape.
4. No image parser, model acquisition or console route is enabled by these types alone.
