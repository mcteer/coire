# Feature Specification: Queued image job settings snapshot

**Feature Branch**: `feat/015g6-image-job-snapshot`
**Parent**: `specs/015-image-generation/` (part of T006/T023/T035/T037)
**Dependency**: draft PR #74

## Goal

Represent a durable queued image job before a Studio runtime and hardware fingerprint are known, without claiming exact reproduction prematurely.

## Acceptance

1. The job response always exposes the immutable effective `ImageSpec`, including its generated seed. It exposes a full `ResolvedImageSpec` only after runtime binding. A queued job may have `resolved=null`; a running/transferring/succeeded job must have a matching resolved spec.
2. The JSON snapshot stored in `ImageJobRow.resolved_spec` is a strict Pydantic shape with effective spec and optional resolved recipe facts. The effective spec never changes after admission. A later bind may fill the resolved member once, with matching spec hash; a conflicting bind is refused.
3. Existing job IDs, state names and receipt shape remain unchanged. Tests cover queued projection, running validation and mismatched/duplicate bind. OpenAPI freshness passes; generated job types appear when the native job route is added.
