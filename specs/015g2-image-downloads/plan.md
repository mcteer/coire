# Implementation Plan: Authenticated image download grants

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API-owned blob reads only; no model/engine/container change. |
| III | Existing `ImageDownloadGrant` and PNG route contracts, generated OpenAPI/TS. |
| IV | Current owner/key/explicit checks plus subject-bound opaque grant; no URL-only access. |
| V | Blob key is data, never an engine path or acquisition request. |
| VI | Fixed-label download spans/metrics and content-free runbook. |
| VII | Grant, blob and route contract tests before implementation. |

## Approach

Hash a random grant token with the current credential binding and persist only that hash. Place the token in a URL fragment, which never reaches the server or ASGI tracing, and require the client to send it as `X-Coire-Image-Grant`. At redemption, lock the output in the request transaction, recheck live explicit policy, validate grant expiry/binding, and open the blob with no-follow directory walking. Hash the file before sending; stream from the authorized descriptor after the transaction closes. Deletion revokes access for every new request.
