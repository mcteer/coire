# Feature Specification: Explicit File Retry

**Feature Branch**: `feat/014ak-file-retry`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AK1: The owner can request one explicit second inspection attempt for a failed attachment, using a unique request ID and expected conversation revision. The original generated key and digest remain unchanged; a second failure is terminal until the file is removed/reuploaded.
- FR-AK2: Retry admission requires the prior failed job's `output_purged` marker. A still-running/processing, deleted, foreign, stale-revision or unsupported operation request is refused. Repeated identical request IDs return the already admitted result without enqueuing again.
- FR-AK3: The owner and conversation quota reservation is reused after verified cleanup. Admission atomically creates attempt two, rebinds the reservation, sets the attachment processing, increments revision and emits a scoped event. It never dispatches the worker from the API.
- FR-AK4: Route and service tests cover owner/Origin, revision, cleanup gate, idempotence and attempt cap. Chat remains default-off until render, file UI, coding, visual and final gates pass.
