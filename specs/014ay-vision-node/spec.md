# Feature Specification: Bare Vision Engine Ownership

**Feature Branch**: `feat/014ay-vision-node`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AY1: Only coire-node starts a registry-selected local `mlx_vlm.server` process. Its argv fixes model path, bind address, port and bounded cache/concurrency; it never includes remote-code trust or a caller path.
- FR-AY2: The backend travels through node start requests, owner status, persisted process state and re-adoption. A duplicate load using a different backend is refused.
- FR-AY3: All engine subprocesses use offline environment settings without Hugging Face credentials or inherited remote-code trust. Existing text argv and readiness behavior remain compatible.
- FR-AY4: Control-plane admin, gateway and placement load callers send the model's registry backend; VLM requests do not send a text chat-template override.

## Acceptance

- Pure argv and mocked process tests prove local model path, bounds, backend recording and re-adoption without starting Metal.
- Full existing node/API tests remain green. Actual tiny-VLM load and visual generation remain parent integration gates.
