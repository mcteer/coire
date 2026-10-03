# Feature Specification: Typed private recipe client

**Feature Branch**: `feat/015h3-image-recipe-client`
**Parent**: `specs/015-image-generation/` (part of T054/T055)
**Dependency**: draft PR #66

## Goal

Let the scheduler call the private recipe parser using its dedicated service token and accept only a response bound to the exact requested input ID, size and digest.

## Acceptance

1. The API client sends a strict `ImageRecipeParseRequest` to the internal file worker and validates `ImageRecipeParseResult` before returning it.
2. A mismatched ID, size, digest or malformed recipe is rejected with a content-free error. The client never logs response bodies, tokens or recipe text.
3. A busy worker has a distinct retryable error; transport, 4xx parse and 5xx failures remain distinguishable without exposing private content.
4. Tests cover success, token header, wrong identity, invalid response, busy, and refused parsing. No public route is enabled in this slice.
