# Feature Specification: Conservative image settings and safe errors

**Feature Branch**: `feat/015b6-image-settings`
**Parent**: `specs/015-image-generation/` (T011)
**Dependency**: `015b5-image-asset-contracts` (draft PR #36)

## Goal

Define conservative, bounded image limits and safe domain errors before any image route is enabled. The default configuration must refuse image admission. Generation inputs and recipe-only imports have separate 10 MiB and 64 MiB caps.

## Acceptance

1. Image enablement defaults false; settings cannot be raised beyond the specified maxima.
2. Limits cover inputs, outputs, metadata, queue, daily allowance, storage safety and idle worker time.
3. Image errors map to safe RFC 9457 problem details with stable codes and statuses.
4. Operator documentation names each new variable, default, maximum and the future compose wiring gate.
