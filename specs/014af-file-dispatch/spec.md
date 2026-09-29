# Feature Specification: Durable Chat File Dispatch

**Feature Branch**: `feat/014af-file-dispatch`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AF1: The scheduler alone scans queued/running chat processing rows and starts one DBOS workflow per immutable ULID job. Worker HTTP calls use the dedicated typed private client.
- FR-AF2: A queued job records its generated output IDs, source digest, deadline and running state before any worker POST. On scheduler restart, a running job queries worker status and never blindly resubmits an original.
- FR-AF3: Busy worker refusal may wait and resubmit because no work was admitted. Network ambiguity, missing status after a crash, parser failure and deadline expiry become visible terminal failures without an automatic crashing-document loop.
- FR-AF4: Result manifests are validated against the recorded input ID/digest, operation, pages and reserved output IDs before a processed manifest is persisted. Deleted attachments/conversations cancel or discard eventual work.
- FR-AF5: Dispatch emits a `coire.scheduler.files.process` span, low-cardinality outcome metric and content-free job/attachment identifiers in logs. API verification and ready publication follow in the next child.

## Independent acceptance

Unit tests cover persisted-before-POST ordering, busy retry, restart status-only recovery, missing/ambiguous failure, manifest mismatch, cancellation and no content leakage. Scheduler source type/lint tests and full Python gates pass. Chat remains default-off pending API publication/purge.
