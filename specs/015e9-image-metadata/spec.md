# Feature Specification: Canonical generated PNG metadata

**Feature Branch**: `feat/015e9-image-metadata`
**Parent**: `specs/015-image-generation/` (T031)
**Dependency**: draft PR #78

## Goal

Write each generated RGB image as a private PNG with an exact, bounded Coire recipe and a digest of its decoded pixels.

## Acceptance

1. Embed exactly one uncompressed `coire.image` iTXt chunk containing `canonical_recipe_bytes(ImageRecipe)`; preserve all effective numeric values and source identities without upstream mflux metadata or local paths.
2. Compute the recipe's pixel digest from RGB bytes and dimensions. Return PNG byte count and SHA-256 for transfer; the file is at most 64 MiB.
3. Create the private output exclusively with restrictive permissions, flush it to disk and delete an incomplete file after any failure. Refuse wrong mode/size or existing destination.
4. A generated output round-trips through the isolated metadata-only recipe parser. Tests use tiny PIL images and no engine or Studio.
