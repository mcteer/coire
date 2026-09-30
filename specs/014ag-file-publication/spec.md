# Feature Specification: Verified File Publication

**Feature Branch**: `feat/014ag-file-publication`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AG1: The API consumes only `processed` worker jobs and verifies their immutable request/result against the owning attachment, source digest, operation, selected pages and reserved output IDs before publication.
- FR-AG2: Every derived asset is read through generated, no-follow keys from the private derived volume and verified for exact byte size, SHA-256, PNG header and declared dimensions before it becomes visible. A missing or mismatched output fails safely.
- FR-AG3: Publication atomically sets ready attachment/job metadata, shrinks its reservation to original plus actual derived bytes, increments conversation revision and emits a scoped attachment event. Deleted parents or attachments never publish.
- FR-AG4: Maintenance scans processed jobs in bounded passes. Observability exposes content-free outcome counts, spans and IDs. Chat remains default-off until previews, retries, cleanup and final acceptance are complete.

## Independent acceptance

Unit tests prove successful text/image/PDF publication, altered or missing asset refusal, owner/tombstone guards, quota accounting and one-time event creation. Full repository gates pass.
