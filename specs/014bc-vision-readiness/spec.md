# Feature Specification: Vision Readiness Generation

**Feature Branch**: `feat/014bc-vision-readiness`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

The bare `mlx-vlm==0.7.3` chat endpoint requires a `model` request field. The node's one-token readiness request must use the verified local store path for a VLM, so a healthy preloaded VLM can transition to `ready`. The text readiness request must keep its existing shape. The field is chosen from the owned engine's slug and never from an API caller.

Acceptance: a mocked vision engine returns 200 only after the local-path generation request, and the node records `ready` without loading weights in tests. Parent tiny-model readiness remains a separate acceptance gate.
