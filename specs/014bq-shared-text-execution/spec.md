# Feature Specification: Shared Text Execution

**Feature Branch**: `feat/014bq-shared-text-execution`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

The compatible OpenAI and Anthropic routes and native Chat share the same registry-based cold-load execution path and canonical text payload adapter. The shared path preserves existing run-token checks, wait ceilings, cancellation, usage accounting and public model identity. Native Chat keeps its owner, durable event and Stop semantics. No route performs loopback HTTP or holds a database transaction during engine I/O.

This slice addresses parent T012. Visual content execution remains in parent T057.
