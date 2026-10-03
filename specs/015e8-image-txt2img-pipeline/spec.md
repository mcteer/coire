# Feature Specification: Fixed Studio txt2img pipeline

**Feature Branch**: `feat/015e8-image-txt2img-pipeline`
**Parent**: `specs/015-image-generation/` (part of T029)
**Dependencies**: draft PR #77 and pinned native runtime in draft PR #31

## Goal

Run a verified local Z-Image Turbo copy through a fixed, resident txt2img pipeline on a Studio. This slice provides the in-process pipeline; node launch, output publication and live-model acceptance remain separate tasks.

## Acceptance

1. Only a preflight-verified local model directory may be loaded. The loader uses mflux `ZImage` with explicit Turbo configuration and never resolves a caller path or downloads weights.
2. Only plain txt2img with zero guidance, no negative prompt, no LoRA/input/control/upscale is accepted. Unsupported values fail before model execution rather than being silently ignored.
3. Each resolved seed produces one RGB PIL image of exact requested dimensions. Progress is emitted only after MLX evaluates the completed denoising step, with bounded step counts and no prompt content in the event.
4. The pipeline requires offline Hub settings before importing mflux. Tests run with a fake engine; no model or real Studio is used in this slice.
