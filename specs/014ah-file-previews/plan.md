# Implementation Plan: Private File Previews

**Branch**: `feat/014ah-file-previews` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a strict `ChatPreviewAsset` core response model and include preview metadata in ready `ChatAttachment` projections. Read previews only through the existing scoped Chat auth dependency and a ready attachment's committed generated manifest. Reuse the API publication verifier to return bounded PNG bytes, then respond with defensive inline image headers. Regenerate OpenAPI and web types and test route authorization and tamper refusal.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | API serves worker-normalized bytes without native parsing or models. |
| III | Shared strict preview metadata and generated OpenAPI/browser types. |
| IV | Owner/parent auth on every read, no token URL, generated manifest keys. |
| VI | Scoped preview outcome metric/span and content-free identifiers. |
| VII | Contract tests, OpenAPI freshness and repository gates. |

No constitution exception or dependency.
