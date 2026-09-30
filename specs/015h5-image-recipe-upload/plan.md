# Implementation Plan: Owner recipe-only upload admission

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API stages bytes, file worker parses metadata; no model work on core. |
| III | Existing `ImageInputUpload`, `ImageInput`, parse command/result and generated OpenAPI/TS. |
| IV | Live owner guard, browser origin, private service token, generated storage key. |
| V | Imported recipe is data; no acquisition side effects. |
| VI | Fixed-label upload and processing spans/metrics, safe errors. |
| VII | Contract/workflow tests before code; preserve disabled default. |

## Approach

Stage actual bytes under the API image namespace, then lock quota and add a processing input row in one transaction. Publish the generated key before commit and remove it on failed commit. The scheduler scans durable processing rows and starts a DBOS workflow keyed by the row's processing ULID; it calls the private parser, then settles the storage hold with the recipe update. Stable input failures remain held until an API cleanup pass unlinks bytes; retryable transport/busy failures leave the row processing.
