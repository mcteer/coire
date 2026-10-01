# Implementation Plan: Failed and orphan image input cleanup

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API-owned file cleanup only; no model work. |
| III | Existing `ImageInputRow` and owner status contract. |
| IV | Generated names and no-follow directory/file handling. |
| V | No acquisition from imported metadata. |
| VI | Fixed-label cleanup metrics and pending-age gauge. |
| VII | Failure and retry tests before code. |

## Approach

Extend API image maintenance. Read a candidate row, lock global and owner quota first, then lock/recheck the input row. Validate counters before unlink. Remove the exact generated file with no-follow operations; after confirmed absence, decrement held bytes and set `purged_at`. Scan old root entries with strict generated-name parsing, query the database for the matching UUID, and remove only unreferenced regular files. Bound each pass and preserve failed rows for status polling.
