# Feature Specification: Image job and compatible projections

**Feature Branch**: `feat/015b2-image-projections`
**Parent**: `specs/015-image-generation/` (remaining T006)
**Dependency**: `015b-image-contracts` (draft PR #32)

## Goal

Complete the strict coire-core image contract set with native job, event, output, preset, grant and compatible API projections. No service route is enabled. Events must identify legal terminal states and compatible requests must reject unsupported output formats or generation modes.

## Acceptance

1. Native job, page, event, output, preset, grant, deletion and recipe-import wire types forbid unknown fields and carry bounded IDs, values and safe errors.
2. Event type and state agree; progress and error events carry required details.
3. Compatible requests support standard fields and only prefixed Coire additions; PNG is the only output format.
4. Compatible success contains one or more URL or base64 entries, plus `coire_job_id`.
5. Shared contract tests pass; OpenAPI and generated web types remain unchanged until routes reference these models.
