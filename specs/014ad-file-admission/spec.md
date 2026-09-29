# Feature Specification: Private Chat Original Admission

**Feature Branch**: `feat/014ad-file-admission`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AD1: A verified owner may upload text/code/PDF/still-image originals to a live conversation through bounded multipart fields and opaque bytes. No caller path, URL or MIME claim becomes a storage key or detected type.
- FR-AD2: Originals are at most 10 MiB. Owner and conversation aggregate quotas reserve the original plus worst-case derivatives under a lock. Refused or failed uploads leave no visible attachment, original or committed quota.
- FR-AD3: Originals use generated UUID keys, private storage, SHA-256 and atomic no-overwrite publication. Filename metadata is a safe basename and never a storage path.
- FR-AD4: The owner can read metadata and download the original through a reauthorized parent-bound route with attachment disposition, `nosniff` and `no-store`. Missing, foreign, deleted and cross-parent IDs share a safe 404.
- FR-AD5: Admission records processing state and an immutable queued processing job plus attachment event. Durable dispatch, result validation, previews, retry, deletion and purge follow in later children. Chat stays default-off until the full path passes.

## Independent acceptance

Contract/storage tests cover ownership, revision and quota, size, generated key, download headers and queued job. A disposable PostgreSQL run verifies attachment/quota/job/event FKs and commit order. API/web image and nginx policy, OpenAPI/browser types, and full repository gates pass.
