# Implementation Plan: Image request and recipe contracts

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Contracts only; no engine or container changes. |
| III | New strict wire types live in coire-core. |
| IV | Recipe excludes identity, paths, URLs and secrets by type. |
| V | Model references are registry UUIDs. |
| VI | No executable path is added; later child adds telemetry. |
| VII | Validation and deterministic hash tests precede implementation. |

## Approach

Use Pydantic v2 `extra="forbid"`, UUID registry references, ULID job identifiers and decimal values for full precision. Keep a fixed version-1 pipeline shape. A resolved type carries immutable manifests and effective seeds. Canonical JSON uses sorted compact UTF-8 with deterministic decimal representation; compare pixel digests separately from PNG bytes. No migration or generated API types are needed until a route references the models.
