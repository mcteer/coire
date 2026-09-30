# Feature Specification: Private image job and preset persistence

**Feature Branch**: `feat/015c1-image-job-schema`
**Parent**: `specs/015-image-generation/` (part of T008–T009)
**Dependency**: `015b6-image-settings` (draft PR #37)

## Goal

Persist immutable preset revisions and private image jobs/events before admission is enabled. Existing text and VLM records must remain valid. A schema downgrade must refuse while image records exist.

## Acceptance

1. Job IDs are ULIDs; owner-scoped idempotency keys and per-job event sequences are unique.
2. Jobs record request intent, resolved spec, version, fence, authorization snapshot and cancellation/publication evidence durably.
3. Preset revisions preserve history with a unique revision per preset and an admin creator.
4. An additive migration follows the current 0022 head and refuses destructive downgrade while image records exist.
