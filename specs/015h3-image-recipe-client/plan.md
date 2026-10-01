# Implementation Plan: Typed private recipe client

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Private API-to-file-worker call only; no model execution. |
| III | Strict coire-core request/result contracts. |
| IV | Dedicated service token and exact identity check. |
| V | Parsed recipe cannot acquire a model through this client. |
| VI | Caller will record fixed-label workflow metrics; client errors contain no content. |
| VII | Failing client tests precede implementation. |

## Approach

Extend the existing `FileWorkerClient` and reuse its owned/client-injected lifetime. Validate the typed worker response, then compare every binding field to the request. Return stable, content-free exception types for scheduler retry policy.
