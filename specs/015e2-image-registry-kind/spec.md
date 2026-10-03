# Feature Specification: Persist image asset kind

**Feature Branch**: `feat/015e2-image-registry-kind`
**Parent**: `specs/015-image-generation/` (part of T027–T028)
**Dependency**: draft PR #49

## Goal

Persist registry asset kind with a backward compatible language default and enforce a valid engine backend for each kind. Existing text/VLM and provider rows must retain their behavior. New image acquisition will use this authoritative field.

## Acceptance

1. Existing registry rows migrate to `language_model` without changing their backend or source.
2. Image base models require Studio `mflux`; auxiliary kinds require Studio `auxiliary`; language rows cannot use those backends.
3. Chat/model resolution checks both kind and backend, including for administrators.
4. A downgrade refuses while non-language registry rows remain; otherwise it removes the column and constraint.
