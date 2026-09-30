# Feature Specification: Gated Inline Visual Preflight

**Feature Branch**: `feat/014bx-inline-visual-preflight`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

For a registry-verified visual model, the compatible `/v1/chat/completions` route may forward one bounded inline PNG to its selected Studio engine when the experimental gateway flag is enabled. Before engine I/O, the gateway checks the model's measured image count, encoded byte and pixel limits and reserves visual context. Text-only models, remote URLs, malformed PNG headers, unsupported formats and excess images are refused. The flag defaults off while file-worker normalization, retained-asset reuse and native Chat image serving remain unfinished. No model, tokenizer or image processor runs on core.
