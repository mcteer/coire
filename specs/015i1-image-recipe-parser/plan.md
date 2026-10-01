# Implementation Plan: Settings-only PNG recipe parser

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I, II | CPU-only file-worker parser; no engine or model on core. |
| II-a | Existing isolated file-worker image; no new service. |
| III | Returns existing strict `ImageRecipe` wire model. |
| IV | Caller must provide an owner-scoped staged path; no path or bytes enter errors/logs. |
| V | Metadata is data and triggers no acquisition or execution. |
| VI | Future route uses existing bounded image telemetry; parser has stable safe error codes. |
| VII | Tests before code; full workspace gates. |

## Approach

Open with `O_NOFOLLOW`, check regular-file size, stream each PNG chunk and CRC in at most 64 KiB blocks. Buffer only a bounded iTXt recipe candidate, require the exact uncompressed key, and validate canonical JSON as `ImageRecipe`. The caller is responsible for staged file ownership and upload quotas.
