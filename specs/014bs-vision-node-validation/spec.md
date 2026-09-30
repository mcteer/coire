# Feature Specification: Local Vision Validation

**Feature Branch**: `feat/014bs-vision-node-validation`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

The node's existing authenticated validation job accepts a registry-selected backend. For a visual backend, it checks a complete local processor and safetensors inventory and runs a one-image generation smoke against the verified local store path with the pinned bare MLX-VLM library, offline and without remote code. A failed or incomplete smoke cannot mark the variant validated. The text validation path remains compatible. The admin visual acquisition refusal remains in place until the scheduler and registry publication flow carry this result end to end.

This slice advances parent T054–T055; it does not yet enable visual acquisition or serving.
