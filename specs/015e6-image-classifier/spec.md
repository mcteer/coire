# Feature Specification: Studio CPU image tagging

**Feature Branch**: `feat/015e6-image-classifier`
**Parent**: `specs/015-image-generation/` (T016, T021)
**Dependency**: draft PR #61

## Goal

Classify completed Studio images with the pinned, admin-acquired safetensors classifier using local Transformers on CPU. The classifier supplies a gallery tag only; it never rejects or edits a prompt or an entitled output.

## Acceptance

1. The local classifier uses `local_files_only=True`, `trust_remote_code=False`, `use_safetensors=True`, CPU tensors, and no Hugging Face credentials or network.
2. The supervisor kills the classifier process after 10 seconds or if its resident memory exceeds its measured reservation, then returns `unknown` with a safe diagnostic.
3. The recorded result includes normal/explicit/unknown tag, threshold 0.5, score when known, pinned classifier revision, processor version, and diagnostic. Policy-explicit always tags explicit even if the classifier reports normal or fails.
4. Bad images, missing assets, model errors, malformed child output, timeout, and memory excess never block publication for an otherwise entitled owner. They produce unknown unless policy-explicit.
5. Unit tests use a fake process; a real-model Studio integration run remains an acceptance gate.
