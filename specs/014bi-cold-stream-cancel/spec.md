# Feature Specification: Cancel Abandoned Cold Loads

**Feature Branch**: `feat/014bi-cold-stream-cancel`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

Closing a compatible or native Chat stream while its model is still loading cancels that stream's pending load task. The caller does not leave an unobserved load workflow behind. A healthy load continues to its normal engine stream and usage accounting.

Acceptance: OpenAI and Anthropic cold streams cancel their pending task when closed after a keepalive; existing Chat cold, Stop and compatible stream regressions pass.
