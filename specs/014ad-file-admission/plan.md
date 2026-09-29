# Implementation Plan: Private Chat Original Admission

**Branch**: `feat/014ad-file-admission` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use the strict 014ac metadata model with flat multipart form fields and a file part. Limit reads to 10 MiB, hash and stage a generated temporary key. Lock the owner row before the conversation and quota sums, recheck revision, insert attachment/job/reservation/event, then atomically publish the original before commit; compensate on failure. Mount the original volume in the API image and Compose with non-root ownership. Provide parent/owner-scoped metadata and attachment downloads; add an 11 MiB nginx envelope solely for this route. The scheduler later consumes the queued job by generated ID/digest. Keep native Chat off until dispatch/publication/purge are built.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | API stores bytes only; CPU parsing stays in worker and model inference on Studios. |
| III | Multipart metadata, attachment and job outputs use strict core contracts; OpenAPI and TS types regenerate. |
| IV | Owner/parent checks, generated keys, no-follow atomic storage, locked quotas and scoped downloads. |
| V | No model acquisition or engine option comes from uploads. |
| VI | Upload outcome counter and content-free span/log identifiers. |
| VII | Contract/storage/quota tests, disposable PostgreSQL proof and image/scan/build gates. |

No constitution exception. `python-multipart==0.0.32` (Apache-2.0) is declared directly for FastAPI multipart parsing; it was already transitively locked via MCP.
