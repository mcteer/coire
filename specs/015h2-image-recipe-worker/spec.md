# Feature Specification: Private recipe parsing handoff

**Feature Branch**: `feat/015h2-image-recipe-worker`
**Parent**: `specs/015-image-generation/` (part of T049/T053/T054)
**Dependency**: draft PR #65

## Goal

Expose the existing metadata-only PNG parser through the authenticated private file worker so a later owner upload workflow can obtain a strict recipe without loading pixels or model code on core.

## Acceptance

1. A strict request contains only a generated UUID, expected SHA-256 and expected byte count. The worker selects the configured `images` namespace and never accepts a path.
2. The worker opens a regular no-follow file <=64 MiB, parses only a bounded `coire.image` iTXt recipe, and verifies SHA-256 on the same open descriptor before returning a strict recipe result.
3. The private route requires the existing file-worker service token, declines work while another conversion is active, uses a bounded deadline, and returns stable content-free failures.
4. Tests cover authentication, valid recipe, hash/size mismatch, symlink, no pixel decoding and busy behavior. The public upload route stays disabled.
