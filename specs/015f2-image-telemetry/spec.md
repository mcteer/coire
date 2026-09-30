# Feature Specification: Content-free image telemetry

**Feature Branch**: `feat/015f2-image-telemetry`
**Parent**: `specs/015-image-generation/` (T013)
**Dependency**: `015f1-image-storage-config` (draft PR #42)

## Goal

Provide bounded OTel spans, counters and durations for future API and Studio image paths. Metric labels must be low-cardinality enums; prompts, recipes, paths and identifiers must never be metric labels. Ship a dashboard and alerts with the telemetry contract.

## Acceptance

1. API and node helpers emit `coire.*` spans and `coire_` metrics with finite operation/stage/outcome/reason labels.
2. Reject caller-supplied arbitrary label strings. Structured logs may include job/model/user IDs, never content or secret material.
3. Dashboard shows image requests, worker stage failures and purge age; alerts cover image failures and overdue purge.
4. No image route is enabled by this slice.
