# Feature Specification: Reasoning Boundary Regression Coverage

**Feature Branch**: `feat/014aw-reasoning-boundaries`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AW1: Tests must prove split and unclosed thinking delimiters, mixed answer/reasoning text, and direct engine reasoning fields are assigned to separate channels.
- FR-AW2: A Stop while an opening delimiter is incomplete must emit no fragment into the answer channel and must finish with a stopped turn.
