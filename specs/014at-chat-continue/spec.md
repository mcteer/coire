# Feature Specification: Continue a Partial Chat Response

**Feature Branch**: `feat/014at-chat-continue`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AT1: The owner may explicitly continue only the latest failed, stopped or interrupted plain-Chat turn when its saved assistant response has nonblank partial text. No continuation starts automatically.
- FR-AT2: Continuation creates a separate user instruction and assistant response linked to the prior turn. The saved partial answer stays visible and is included in model history so the model can continue from it. A repeated request ID follows the existing turn without a second generation.
- FR-AT3: A continuation cannot change the fixed continuation instruction or attach new files. The API rechecks owner, model and context eligibility under the existing admission lock.
- FR-AT4: The browser offers a distinct **Continue from partial answer** action and preserves an unrelated unsent draft. Retry remains a separate action with its original-input semantics.

## Acceptance

- A partial terminal response can be continued with one additional user and assistant message.
- Empty partials, changed instructions, file changes, stale targets and unauthorized access are refused.
- The saved partial and a separate draft survive a successful continuation; OpenAPI and generated browser types match the core contract.
