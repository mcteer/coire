# Feature Specification: Authenticated node image worker routes and shared budget

**Feature Branch**: `feat/015e16-image-node-routes`
**Parent**: `specs/015-image-generation/` (part of T024/T030/T033)
**Dependency**: draft PR #85

## Goal

Expose the Studio image supervisor through the authenticated node control listener and make its memory hold visible to existing language engine and reservation admission.

## Acceptance

1. Typed worker load/status/unload routes require the existing node bearer and are absent from the data listener. Status probes authenticated worker readiness; load and stop run off the event loop. Wrong instance and unavailable/uncertain states return safe errors.
2. A shared lock covers both language engine and image worker memory admission. Engine starts include the image reservation; image starts include language engine reservations. The node reservation ledger sees the sum.
3. On agent restart, image process re-adoption runs before listeners bind. Unknown state retains a conservative hold and is logged without exposing secrets.
4. Contract tests cover route authentication/shape/listener isolation and a budget regression; no native engine or Studio is run.
