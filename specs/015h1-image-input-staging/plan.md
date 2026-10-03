# Implementation Plan: Bounded image input staging

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Core only stores bytes; no engine or model work. |
| III | Existing strict `ImageInputUpload` governs declared metadata. |
| IV | Future route supplies authenticated owner; generated private paths prevent caller path selection. |
| V | No model lookup or acquisition. |
| VI | Future route will call existing image telemetry seam; helper records no content. |
| VII | Test bounds and cleanup before implementation; keep this isolated slice green. |

## Approach

Use a dedicated image namespace under the API original volume. Read `UploadFile` in 64 KiB pieces with the validated purpose cap; write to an exclusive 0600 temporary file, hash actual bytes, fsync, then return a staged handle. The admission route will atomically publish after quota and DB checks. Cleanup is idempotent.
