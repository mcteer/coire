# Feature Specification: Image request and recipe contracts

**Feature Branch**: `feat/015b-image-contracts`
**Parent**: `specs/015-image-generation/` (T004, T010; first part of T006)

## Goal

Define strict shared Pydantic image request, capability, input, resolution and recipe contracts before any image route or worker is added. Validate fixed image modes and bounded inputs. Canonical recipe and client-intent hashes must be deterministic, precise, and free of identity, paths and secrets. Job, event, preset, grant and compatible API projections follow in a separate child.

## Acceptance

1. A root-level submit request accepts optional overrides and a preset reference; unknown fields are errors.
2. Mode-specific source, mask, control and strength rules reject invalid combinations; dimensions, steps, guidance, seeds, output count and LoRA scales are conservatively bounded.
3. Generation inputs are at most 10 MiB; recipe-only PNG imports may be 64 MiB; recipe metadata is at most 64 KiB.
4. Resolved recipes retain exact numeric values, immutable dependency/input revisions, output seed and pixel digest, without local paths, URLs or identity.
5. Canonical client intent is stable across JSON field order, and seed expansion is deterministic modulo 2^32.
6. No route is enabled by this child.
