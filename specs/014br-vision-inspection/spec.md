# Feature Specification: Preconverted Vision Inspection

**Feature Branch**: `feat/014br-vision-inspection`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

An admin acquisition request may inspect a supported preconverted MLX-VLM repository before any weights move. Inspection identifies the backend from its declared architecture and MLX metadata, verifies the local processor/tokenizer file inventory, and refuses raw, unsupported or incomplete visual repositories with typed safe reasons. Until the separate visual validation and publication path is complete, even a complete visual repository receives an audited refusal before weight transfer. Ordinary users and Chat cannot trigger acquisition.

This slice advances parent T054–T055; it does not enable visual acquisition or serving by itself.
