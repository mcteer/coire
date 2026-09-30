# Feature Specification: Image registry kinds

**Feature Branch**: `feat/015b3-image-registry-contracts`
**Parent**: `specs/015-image-generation/` (part of T005 and T007)
**Dependency**: `015b2-image-projections` (draft PR #33)

## Goal

Extend shared registry contracts with distinct image base, adapter, control, upscaler and classifier kinds. Preserve the legacy language-model default. Only a generation base may use the mflux engine backend; auxiliary assets cannot be started as engines or enter a chat listing by type alone.

## Acceptance

1. Existing registry records without `kind` remain language models and retain text/VLM backends.
2. Image generation bases have an mflux backend and measured image capability before publication.
3. Auxiliary image assets have a non-routable backend marker and cannot pass an engine-start request.
4. Provider targets remain language models; image assets are Studio-local registry UUIDs.
5. Strict contract tests cover incompatible kind/backend combinations and legacy defaults.
