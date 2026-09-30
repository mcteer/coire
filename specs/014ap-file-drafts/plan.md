# Implementation Plan: Same-Tab File Drafts

**Branch**: `feat/014ap-file-drafts` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the existing sessionStorage draft codec with validated generated file selection shapes. Reconstruct a strict text/model/file projection before writing or loading, so arbitrary fields cannot persist. Save selections on change and conversation navigation; reconcile restored IDs and pages against owner-scoped conversation detail before showing them. Keep visual Send disabled. Test identity clearing, malformed drafts, restored PDF pages and the ten-file UI bound.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Uses generated `ChatAttachmentSelection` type; no wire change. |
| IV | Owner-keyed same-tab storage, no bytes/tokens; server confirms files on reopen. |
| VII | Storage and Chat browser tests, lint, build. |

No constitution exception or dependency.
