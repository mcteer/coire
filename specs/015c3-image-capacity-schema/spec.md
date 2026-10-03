# Feature Specification: Image capacity and lease persistence

**Feature Branch**: `feat/015c3-image-capacity-schema`
**Parent**: `specs/015-image-generation/` (part of T008–T009)
**Dependency**: `015c2-image-asset-schema` (draft PR #39)

## Goal

Persist owner/global image quota counters, fenced node execution leases, and measured coexistence profiles before image admission is enabled.

## Acceptance

1. A single quota row exists per owner and a single global row. Counters cannot be negative, and daily output counts carry a UTC day bucket.
2. Execution leases bind exactly one image job or inference request to a node, with a fence, heartbeat, expiry and release evidence.
3. Coexistence profiles bind a node, runtime/hardware fingerprint, chat variants and image model to a measured status.
4. An additive migration follows 0024 and refuses destructive downgrade while capacity records exist.
