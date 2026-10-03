# Feature Specification: Admin-imported image preset templates

**Feature Branch**: `feat/015d8-image-preset-templates`
**Parent**: `specs/015-image-generation/` (completes T020 with prior slices)
**Dependency**: draft PR #56

## Goal

Ship small, inert image preset templates that an administrator can bind to a verified registry model and create through the audited admin preset API. A template is data, never an acquisition command or a publication shortcut.

## Acceptance

1. Templates contain only allowed `ImagePresetCreate` fields and an explicit model-ID slot.
2. Replacing the slot with a UUID yields a strict valid contract; the unbound template is rejected by the contract and cannot be posted accidentally.
3. Templates have no entitlement, dependency, URL, path or acquisition field.
4. Import instructions use the existing audited POST route and state that model kind/readiness and live policy are checked there.
