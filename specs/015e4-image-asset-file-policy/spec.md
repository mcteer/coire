# Feature Specification: Image asset file policy

**Feature Branch**: `feat/015e4-image-asset-file-policy`
**Parent**: `specs/015-image-generation/` (part of T027)
**Dependency**: draft PR #58

## Goal

Give the dedicated admin image acquisition path a fail-closed metadata check and exact download allowlist. Image assets may contain safetensors and inert configuration files, never executable code, pickle weights, symlink-like paths, or unpinned revisions. The classifier is pinned to the revision and weight digest selected in the parent research.

## Acceptance

1. File manifests must have safe relative paths, no duplicates, at least one safetensors weight, and a resolved 40-character commit. Executable/pickle/archive files present upstream are excluded from the exact download list; unsafe paths fail inspection.
2. Base and classifier assets require local config; classifier also requires a preprocessor config, exact pinned revision, safetensors byte count and upstream SHA-256.
3. A snapshot helper passes only validated exact safe file paths to Hugging Face and never performs an unfiltered image download. The pinned classifier repository has `.pt` and `.bin` siblings; neither may be fetched.
4. The existing language acquisition path is unchanged. Full image acquisition, licensing, local component closure and publication remain separate parent work.
