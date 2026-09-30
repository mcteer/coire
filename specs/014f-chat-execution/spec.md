# Feature Specification: Shared Gateway Stream Execution

**Feature Branch**: `feat/014f-chat-execution`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-F1: The existing compatible `/v1` streams preserve their exact OpenAI and Anthropic wire behavior, including public model identity, keepalive, terminal handling, credential recheck and once-only usage accounting.
- FR-F2: Stream tracking, detached cancellation accounting and model identity rewriting are reusable gateway operations, so the later native Chat route can consume the same upstream proxy and accounting path without an HTTP loopback.
- FR-F3: Engine proxy leases, saturation, timeout and abort behavior remain in the existing gateway proxy; extraction does not create a new engine or ingress path.
- FR-F4: No transaction stays open for engine I/O, and a disconnected controlling stream closes its upstream source.

## Independent acceptance

Regression tests exercise fragmented SSE usage, first-token timing, credential revocation, cancellation/abort, model rewriting and existing `/v1` contract responses. No new native route is introduced in this child.
