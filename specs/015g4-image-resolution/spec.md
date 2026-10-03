# Feature Specification: Image submission settings resolution

**Feature Branch**: `feat/015g4-image-resolution`
**Parent**: `specs/015-image-generation/` (part of T035)
**Dependency**: draft PR #72

## Goal

Produce a complete, validated `ImageSpec` for a basic native submission before any job or quota mutation.

## Acceptance

1. A measured image capability profile carries explicit default width, height, steps and guidance. New fields remain optional for reading old rows, but admission refuses an incomplete profile. Defaults must lie within the measured bounds.
2. Direct and preset-overlaid requests resolve these defaults under strict `ImageSpec` validation, then the measured profile validates mode, dimensions, steps, guidance and count. No field is silently clamped or ignored.
3. A missing seed receives one cryptographically random unsigned 32-bit value; a supplied seed is retained. The resulting spec is immutable and its canonical hash is stable.
4. This slice supports txt2img only. Advanced modes, LoRA, control, upscale, model-variant and input bindings are refused until their registry and worker validation paths exist. The public job route remains unavailable.
5. Tests cover defaults, explicit overrides, invalid bounds, missing defaults, unsupported fields and seed preservation. Generated contracts stay current.
