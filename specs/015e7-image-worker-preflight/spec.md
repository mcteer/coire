# Feature Specification: Studio image worker copy preflight

**Feature Branch**: `feat/015e7-image-worker-preflight`
**Parent**: `specs/015-image-generation/` (part of T024/T029/T030/T033)
**Dependencies**: draft PR #76 and the native runtime in draft PR #31

## Goal

Let coire-node identify an admin-acquired local image copy by registry slug and verify its exact manifest and safe files before a resident mflux worker can load it.

## Acceptance

1. `ImageWorkerLoadRequest` includes a registry-resolved slug; it never accepts a caller filesystem path. Invalid or traversing slugs are refused by the shared wire contract. OpenAPI freshness passes; this shape appears in generated API types when its node route is exposed.
2. The Studio node refuses a missing copy/manifest, digest mismatch, wrong runtime version, symlink/special files or unsafe image asset suffix. It checks every manifest file's size and digest and rejects extra content using the existing store verifier. Nothing triggers a Hub pull.
3. Preflight returns only the verified local Store path. It does not launch a process or mark the model ready. Tests use tiny local file trees and forged manifests; no real Studio or engine runs.
4. This slice leaves image worker launch and job execution open; the local runtime dependency comes from PR #31, which must merge before the live worker path is enabled.
