# Feature Specification: Admin Vision Acquisition Publication

**Feature Branch**: `feat/014bt-vision-acquisition-publication`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

An authenticated admin may acquire a complete, supported, already-converted MLX visual repository. Inspection rejects unsupported recipes and architectures before transfer. The scheduler carries the inspected backend to node validation, persists the measured visual capability only after a passing local image smoke and matching replica checksums, and keeps failed variants unpublished. Text acquisition remains compatible. All mutations use the existing audit trail and only coire-node accesses model files.

Parent T054–T055 remain open until local tiny-model integration and end-to-end acquisition tests pass.
