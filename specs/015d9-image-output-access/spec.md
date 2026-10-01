# Feature Specification: Image output access policy

**Feature Branch**: `feat/015d9-image-output-access`
**Parent**: `specs/015-image-generation/` (part of T014, T017, T022)
**Dependency**: draft PR #57

## Goal

Give gallery and download routes one reusable policy check for published owner output. A download rechecks the current user, personal key, and explicit permission, even if a short-lived grant was issued earlier. A future shared view can include only standard, normally tagged output. This child introduces no sharing or download route.

## Acceptance

1. A deleted, unpublished, or other-owner output cannot be downloaded by an ordinary user or admin.
2. Explicit content, whether marked by policy or classifier, requires live `explicit` entitlement and the personal key's `images:explicit` scope on every download. A stale principal claim is insufficient.
3. Unknown content remains available to its entitled owner but is excluded from any shared view. Explicit content is also excluded, including a row whose classifier tag is normal but policy mode is explicit.
4. Missing or unrecognized policy metadata fails closed; no prompt or recipe enters an audit or log record from this guard.
