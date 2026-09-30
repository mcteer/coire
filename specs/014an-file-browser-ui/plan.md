# Implementation Plan: Browser File Preparation

**Branch**: `feat/014an-file-browser-ui` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use generated core-derived OpenAPI types in the existing Chat API module. Send multipart uploads with same-origin credentials and no JSON header. Keep attachment state in the conversation hook, reconcile revisions from owner-scoped detail, and poll processing files through owner-scoped metadata reads. Add a file panel for upload, status, retry, explicit PDF page rendering, original download and PNG previews. Block selected-file sends until the backend turn path is ready. Cover the multipart boundary and page/preview behavior with browser tests.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Generated `ChatAttachment` and `ChatFileProcessRequest` types; existing API routes. |
| IV | Same-origin credentials and owner-scoped paths; no token URL. |
| VI | Existing API/worker file spans and metrics cover browser operations. |
| VII | Client, component and Chat hook tests plus web build/lint. |

No constitution exception or dependency.
