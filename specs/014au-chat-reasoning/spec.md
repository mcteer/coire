# Feature Specification: Separate Native Chat Reasoning

**Feature Branch**: `feat/014au-chat-reasoning`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AU1: For a model whose capability profile declares thinking or hybrid reasoning, split answer and reasoning incrementally, including delimiters divided across engine frames. An unclosed thinking block must never enter the answer channel.
- FR-AU2: Persist reasoning in its own bounded message field and emit typed reasoning deltas before browser delivery. Keep the same owner-only event and message access controls.
- FR-AU3: The browser shows saved or streamed reasoning in a collapsed disclosure. Reasoning text is inert; it is not interpreted as HTML or fetched as remote content.
- FR-AU4: A model with no declared reasoning retains plain text behavior. Profile values, not model names, control parsing.
