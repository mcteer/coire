# Feature Specification: Compatible Multimodal Contracts

**Feature Branch**: `feat/014c-compatible-contracts`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-C1: Existing `/v1` text and null messages, tool-call extra fields, and text engine defaults remain valid. OpenAI text/image parts are typed; inline images are bounded data URIs. HTTP/file image URLs and unknown part types are refused by the core contract.
- FR-C2: Registry, variants, node engine start/status and reconciliation carry an additive backend discriminator defaulting to text. Measured visual limits are server-controlled, absent by default, and unavailable in admin curation input.
- FR-C3: Harness run requests can carry bounded trusted image asset references separately from editable task text. Node activity metadata is typed and bounded without tool argument/result content.
- FR-C4: No model path, remote URL, remote-code flag, or entitlement bypass is introduced. Existing text and admin contract tests remain green.

## Independent acceptance

Core schema tests cover valid text/null/image, invalid remote and oversized images, backend defaults, visual bounds, harness and activity strictness. Ruff, strict mypy, core and existing gateway/node contract tests pass. Runtime handling follows later child changes.
