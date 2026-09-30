# Feature Specification: Durable image job admission transaction

**Feature Branch**: `feat/015g7-image-admission`
**Parent**: `specs/015-image-generation/` (part of T023/T035/T037)
**Dependency**: draft PR #75

## Goal

Create a queued basic txt2img job with one replay-safe intent key, live policy checks, capacity reservation, first event and audit in one database transaction.

## Acceptance

1. A caller-supplied idempotency key is bounded and stored per owner. The service hashes canonical client intent before generating defaults or seed. A matching replay returns the existing job without reserving capacity or adding an audit row; a changed intent with the same key returns 409.
2. New direct or preset submissions use only live ready Studio image registry records, their measured capability profile, hidden dependency policy and current user/key/entitlement state. The resulting basic `ImageSpec` is immutable. Unsupported advanced fields and unavailable model defaults refuse admission.
3. Under a transaction advisory lock, the service reserves queue/daily/disk capacity, inserts a queued job and typed settings snapshot, appends sequence-one queued event and content-free audit row, then commits before returning a `ImageJobReceipt`. The service stores no credential secret or prompt in audit/log labels.
4. Image admission has no public submit route in this slice. Worker, publication, cancellation and real PostgreSQL contention evidence remain required before any route can enable job creation.
5. Tests cover key replay/conflict, one-time quota/audit charge, live policy refusal and stored snapshot/event content. Local gates pass.
