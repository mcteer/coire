# Feature Specification: Image routing isolation

**Feature Branch**: `feat/015e1-image-routing-isolation`
**Parent**: `specs/015-image-generation/` (part of T027–T028)
**Dependency**: draft PR #48

## Goal

Keep image and auxiliary engine backends out of all existing language/vision model listings and resolution. Until the image acquisition path is ready, legacy language acquisition must reject a non-language `kind` instead of silently treating it as a language model.

## Acceptance

1. Published image and auxiliary rows are absent from user and admin chat/model listings.
2. Direct gateway resolution rejects image/auxiliary rows with the same not-found result as an unavailable language model before engine selection.
3. Legacy admin add rejects image kinds before node inspection or acquisition and audits the refusal.
4. Existing language and vision models remain eligible.
