# Feature Specification: Honest Gateway Visual Refusal

**Feature Branch**: `feat/014az-gateway-visual-guard`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AZ1: Until bounded temporary image processing and VLM visual budgeting are connected, `/v1/chat/completions` refuses inline image parts before spend or engine dispatch. HTTP/file image URLs remain invalid under the core wire schema.
- FR-AZ2: OpenAI text content parts count their actual characters in preflight, rather than the number of parts.
- FR-AZ3: `/v1/messages` refuses image and other unsupported Anthropic content blocks before model resolution. No unsupported block may be silently converted to empty text.
