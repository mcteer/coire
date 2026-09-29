# Feature Specification: Safe Public Model Rewrite

**Feature Branch**: `feat/014bf-gateway-stream-rewrite`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

The compatible OpenAI SSE route must publish the registry model ID instead of a node-local model path even when engine events are fragmented across transport chunks. Malformed or oversized engine events, and events that carry the private path elsewhere, fail closed without forwarding that path. A malformed frame records failed usage and closes upstream promptly. Existing `/v1` streaming behavior and native Chat accounting remain compatible.

Acceptance: fragmented CRLF/multiline model events, malformed events, private-path error events and gateway route regressions pass without starting an engine.
